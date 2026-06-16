"""
Schema object extraction and embedding for performance analysis.

A "schema object" is any entity that can appear in multiple places in code
and whose cardinality matters for performance reasoning:

  - Database tables (from Postgres information_schema)
  - Python classes/dataclasses from the call graph index

Each object is embedded as a structured description that captures:
  - What it represents
  - What it contains (columns / methods / fields)
  - What it relates to (FK references, inheritance)
  - Its cardinality class: SCALAR | LOW | MEDIUM | HIGH | UNBOUNDED

Cardinality class meanings:
  SCALAR    — single value, not a collection (e.g. a config object)
  LOW       — bounded small set, typically < 100 rows (e.g. projects, users)
  MEDIUM    — grows with usage, typically 100–10K (e.g. decisions, contracts)
  HIGH      — scales with data, typically > 10K (e.g. nodes, edges, embeddings)
  UNBOUNDED — grows without bound, no natural cap

When two HIGH/UNBOUNDED objects both appear in a nested call pattern, the
pattern has O(n²) or worse potential and is flagged with high confidence.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB
    from .embeddings.embedder import EmbeddingStore

# ── Cardinality heuristics ────────────────────────────────────────────────────

# Tables whose cardinality is domain-known regardless of row count
_KNOWN_CARDINALITY: dict[str, str] = {
    # Phronosis DB tables
    "projects":                    "LOW",
    "nodes":                       "HIGH",
    "edges":                       "HIGH",
    "function_embeddings":         "HIGH",
    "decision_embeddings":         "MEDIUM",
    "decisions":                   "MEDIUM",
    "decision_functions":          "MEDIUM",
    "contracts":                   "LOW",
    "contract_examples":           "LOW",
    "contract_violations":         "MEDIUM",
    "agent_improvements":          "MEDIUM",
    "project_home_snapshots":      "LOW",
    "dependency_fingerprints":     "LOW",
    "users":                       "LOW",
    "api_keys":                    "LOW",
    "project_access":              "LOW",
    "demo_projects":               "LOW",
    # Common Python collection classes
    "list":     "HIGH",
    "dict":     "HIGH",
    "set":      "HIGH",
    "tuple":    "MEDIUM",
    "str":      "SCALAR",
    "int":      "SCALAR",
    "bool":     "SCALAR",
    "None":     "SCALAR",
}

def _cardinality_from_row_count(count: int) -> str:
    if count < 10:
        return "LOW"
    if count < 1_000:
        return "MEDIUM"
    if count < 100_000:
        return "HIGH"
    return "UNBOUNDED"


# ── SchemaObject ─────────────────────────────────────────────────────────────

@dataclass
class SchemaObject:
    name: str                    # table name or class name
    source: str                  # "db_table" | "python_class"
    project_id: str
    cardinality: str             # SCALAR | LOW | MEDIUM | HIGH | UNBOUNDED
    description: str             # structured text for embedding
    references: list[str] = field(default_factory=list)   # tables/classes this references
    referenced_by: list[str] = field(default_factory=list)
    embedding: list[float] | None = None


def _table_description(
    table: str,
    columns: list[dict],
    references: list[str],
    referenced_by: list[str],
    cardinality: str,
    row_count: int | None,
) -> str:
    col_summary = ", ".join(
        f"{c['name']} ({c['type']})"
        for c in columns[:20]
    )
    parts = [
        f"Database table: {table}",
        f"Cardinality: {cardinality}" + (f" (~{row_count} rows)" if row_count else ""),
        f"Columns: {col_summary}",
    ]
    if references:
        parts.append(f"References (many-to-one): {', '.join(references)}")
    if referenced_by:
        parts.append(f"Referenced by (one-to-many): {', '.join(referenced_by)}")
    return "\n".join(parts)


def _class_description(
    class_name: str,
    methods: list[str],
    docstring: str,
    cardinality: str,
) -> str:
    parts = [
        f"Python class: {class_name}",
        f"Cardinality: {cardinality}",
    ]
    if docstring:
        parts.append(f"Purpose: {docstring[:200]}")
    if methods:
        parts.append(f"Methods: {', '.join(methods[:15])}")
    return "\n".join(parts)


# ── Extraction ────────────────────────────────────────────────────────────────

async def extract_db_schema_objects(db: "CallGraphDB", project_id: str) -> list[SchemaObject]:
    """
    Query Postgres information_schema to build SchemaObjects for every table.
    Also queries pg_class for live row count estimates.
    """
    objects: list[SchemaObject] = []

    # Columns per table
    async with db._db.execute(
        """
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
        """
    ) as cur:
        col_rows = await cur.fetchall()

    tables: dict[str, list[dict]] = {}
    for r in col_rows:
        tables.setdefault(r["table_name"], []).append(
            {"name": r["column_name"], "type": r["data_type"]}
        )

    # FK relationships
    async with db._db.execute(
        """
        SELECT
            tc.table_name  AS from_table,
            ccu.table_name AS to_table
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
        JOIN information_schema.referential_constraints rc
            ON tc.constraint_name = rc.constraint_name
        JOIN information_schema.key_column_usage ccu
            ON rc.unique_constraint_name = ccu.constraint_name
        WHERE tc.constraint_type = 'FOREIGN KEY'
          AND tc.table_schema = 'public'
        """
    ) as cur:
        fk_rows = await cur.fetchall()

    refs_out: dict[str, list[str]] = {}   # table → tables it references
    refs_in: dict[str, list[str]] = {}    # table → tables that reference it
    for r in fk_rows:
        refs_out.setdefault(r["from_table"], []).append(r["to_table"])
        refs_in.setdefault(r["to_table"], []).append(r["from_table"])

    # Row count estimates from pg_class
    async with db._db.execute(
        """
        SELECT relname AS table_name, reltuples::BIGINT AS row_estimate
        FROM pg_class
        WHERE relkind = 'r' AND relnamespace = (
            SELECT oid FROM pg_namespace WHERE nspname = 'public'
        )
        """
    ) as cur:
        count_rows = await cur.fetchall()

    row_counts = {r["table_name"]: r["row_estimate"] for r in count_rows}

    for table_name, columns in tables.items():
        row_count = row_counts.get(table_name)
        cardinality = _KNOWN_CARDINALITY.get(
            table_name,
            _cardinality_from_row_count(row_count) if row_count is not None else "MEDIUM"
        )
        references = list(dict.fromkeys(refs_out.get(table_name, [])))
        referenced_by = list(dict.fromkeys(refs_in.get(table_name, [])))

        desc = _table_description(
            table_name, columns, references, referenced_by, cardinality, row_count
        )
        objects.append(SchemaObject(
            name=table_name,
            source="db_table",
            project_id=project_id,
            cardinality=cardinality,
            description=desc,
            references=references,
            referenced_by=referenced_by,
        ))

    return objects


async def extract_python_class_objects(db: "CallGraphDB", project_id: str) -> list[SchemaObject]:
    """
    Pull class nodes from the call graph and build SchemaObjects for each.
    Methods are found by looking for nodes in the same module that call the class.
    """
    async with db._db.execute(
        """
        SELECT id, name, module, docstring, signature
        FROM nodes
        WHERE project_id = ? AND type = 'class'
        """,
        (project_id,),
    ) as cur:
        class_rows = await cur.fetchall()

    async with db._db.execute(
        """
        SELECT id, name, module
        FROM nodes
        WHERE project_id = ? AND type = 'function'
        """,
        (project_id,),
    ) as cur:
        fn_rows = await cur.fetchall()

    # Group methods by class prefix (module.ClassName.method_name)
    class_methods: dict[str, list[str]] = {}
    for fn in fn_rows:
        parts = fn["id"].split(".")
        if len(parts) >= 2:
            parent = ".".join(parts[:-1])
            class_methods.setdefault(parent, []).append(fn["name"])

    objects = []
    for cls in class_rows:
        methods = class_methods.get(cls["id"], [])
        cardinality = _KNOWN_CARDINALITY.get(cls["name"], "MEDIUM")
        desc = _class_description(
            cls["name"],
            methods,
            cls["docstring"] or "",
            cardinality,
        )
        objects.append(SchemaObject(
            name=cls["name"],
            source="python_class",
            project_id=project_id,
            cardinality=cardinality,
            description=desc,
        ))

    return objects


# ── Embedding + storage ───────────────────────────────────────────────────────

async def embed_and_store_schema_objects(
    objects: list[SchemaObject],
    embeddings: "EmbeddingStore",
    db: "CallGraphDB",
    project_id: str,
) -> int:
    """Embed all objects and upsert into schema_object_embeddings table."""
    if not objects:
        return 0

    await db._db.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_object_embeddings (
            project_id   TEXT NOT NULL,
            name         TEXT NOT NULL,
            source       TEXT NOT NULL,
            cardinality  TEXT NOT NULL,
            description  TEXT NOT NULL,
            references   TEXT NOT NULL DEFAULT '[]',
            referenced_by TEXT NOT NULL DEFAULT '[]',
            embedding    vector(1536),
            PRIMARY KEY (project_id, name, source)
        )
        """
    )
    await db._db.commit()

    texts = [o.description for o in objects]
    vecs = await embeddings._embedder._embed_batch(texts)

    rows = []
    for obj, vec in zip(objects, vecs):
        obj.embedding = vec
        rows.append((
            project_id,
            obj.name,
            obj.source,
            obj.cardinality,
            obj.description,
            json.dumps(obj.references),
            json.dumps(obj.referenced_by),
            vec,
        ))

    await db._db.executemany(
        """
        INSERT INTO schema_object_embeddings
            (project_id, name, source, cardinality, description, references, referenced_by, embedding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project_id, name, source) DO UPDATE SET
            cardinality=excluded.cardinality,
            description=excluded.description,
            references=excluded.references,
            referenced_by=excluded.referenced_by,
            embedding=excluded.embedding
        """,
        rows,
    )
    await db._db.commit()
    return len(objects)


async def load_schema_objects(db: "CallGraphDB", project_id: str) -> list[SchemaObject]:
    """Load previously embedded schema objects for a project."""
    try:
        async with db._db.execute(
            """
            SELECT name, source, cardinality, description, references, referenced_by, embedding
            FROM schema_object_embeddings
            WHERE project_id = ?
            """,
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
    except Exception:
        return []

    return [
        SchemaObject(
            name=r["name"],
            source=r["source"],
            project_id=project_id,
            cardinality=r["cardinality"],
            description=r["description"],
            references=json.loads(r["references"]),
            referenced_by=json.loads(r["referenced_by"]),
            embedding=list(r["embedding"]) if r["embedding"] else None,
        )
        for r in rows
    ]


# ── Main entry point ──────────────────────────────────────────────────────────

async def index_schema_objects(
    db: "CallGraphDB",
    embeddings: "EmbeddingStore",
    project_id: str,
    include_db_tables: bool = True,
) -> dict:
    """
    Extract, embed, and store all schema objects for a project.
    Call this after index_project to build the object embedding layer.
    """
    objects: list[SchemaObject] = []

    if include_db_tables:
        db_objects = await extract_db_schema_objects(db, project_id)
        objects.extend(db_objects)
        print(f"[schema] {len(db_objects)} DB table objects extracted")

    class_objects = await extract_python_class_objects(db, project_id)
    objects.extend(class_objects)
    print(f"[schema] {len(class_objects)} Python class objects extracted")

    count = await embed_and_store_schema_objects(objects, embeddings, db, project_id)
    print(f"[schema] {count} schema objects embedded and stored")

    return {
        "project_id": project_id,
        "db_tables": len([o for o in objects if o.source == "db_table"]),
        "python_classes": len([o for o in objects if o.source == "python_class"]),
        "total": count,
    }
