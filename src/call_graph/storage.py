from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiosqlite

from .models import GraphData
from .parser import CallEdge, FunctionNode

# DDL for fresh installs — uses multi-project schema from the start.
# Existing single-project DBs are upgraded by _migrate_to_multi_project().
#
# NOTE: project_id-dependent indexes are NOT in this DDL — they are created
# in _ensure_indexes() which runs after migration, so they work on both
# fresh installs (project_id in schema) and post-migration upgraded DBs.
DDL = """
CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    root         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    last_indexed TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    project_id  TEXT NOT NULL DEFAULT 'default',
    id          TEXT NOT NULL,
    file        TEXT NOT NULL,
    module      TEXT NOT NULL,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    signature   TEXT NOT NULL DEFAULT '',
    docstring   TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT '',
    body_hash      TEXT NOT NULL DEFAULT '',
    decorators     TEXT NOT NULL DEFAULT '[]',
    embedding_model TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  TEXT NOT NULL DEFAULT 'default',
    caller_id   TEXT NOT NULL,
    callee_id   TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    file        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON edges(callee_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file   ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name   ON nodes(name);

CREATE TABLE IF NOT EXISTS decisions (
    id                   TEXT PRIMARY KEY,
    project_id           TEXT NOT NULL DEFAULT 'default',
    type                 TEXT NOT NULL,
    description          TEXT NOT NULL,
    rejected_alternatives TEXT NOT NULL DEFAULT '',
    trigger              TEXT NOT NULL DEFAULT '',
    parent_decision_id   TEXT,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_functions (
    decision_id  TEXT NOT NULL,
    function_id  TEXT NOT NULL,
    PRIMARY KEY (decision_id, function_id)
);

CREATE INDEX IF NOT EXISTS idx_df_function ON decision_functions(function_id);

CREATE TABLE IF NOT EXISTS contracts (
    id                   TEXT PRIMARY KEY,
    project_ids          TEXT NOT NULL DEFAULT '[]',
    title                TEXT NOT NULL,
    natural_language     TEXT NOT NULL,
    rule_type            TEXT NOT NULL DEFAULT 'SEMANTIC',
    structural_expression TEXT NOT NULL DEFAULT '{}',
    threshold            REAL NOT NULL DEFAULT 0.85,
    status               TEXT NOT NULL DEFAULT 'draft',
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_examples (
    id           TEXT PRIMARY KEY,
    contract_id  TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    example_type TEXT NOT NULL,
    code         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cex_contract ON contract_examples(contract_id);

CREATE TABLE IF NOT EXISTS contract_violations (
    id             TEXT PRIMARY KEY,
    contract_id    TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    function_id    TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    score          REAL NOT NULL DEFAULT 0.0,
    detected_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cviol_contract   ON contract_violations(contract_id);
CREATE INDEX IF NOT EXISTS idx_cviol_project    ON contract_violations(project_id);
CREATE INDEX IF NOT EXISTS idx_cviol_function   ON contract_violations(function_id);

CREATE TABLE IF NOT EXISTS agent_improvements (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL,
    description        TEXT NOT NULL,
    affected_functions TEXT NOT NULL DEFAULT '[]',
    severity           TEXT NOT NULL DEFAULT 'medium',
    suggested_fix      TEXT NOT NULL DEFAULT '',
    reproduction_steps TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'open',
    filed_at           TEXT NOT NULL,
    resolved_at        TEXT,
    resolution_notes   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_impr_project ON agent_improvements(project_id);
CREATE INDEX IF NOT EXISTS idx_impr_status  ON agent_improvements(status);

CREATE TABLE IF NOT EXISTS project_home_snapshots (
    project_id   TEXT PRIMARY KEY,
    hashes       TEXT NOT NULL DEFAULT '{}',
    captured_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependency_fingerprints (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    captured_at      TEXT NOT NULL,
    fingerprint_hash TEXT NOT NULL,
    snapshot_json    TEXT NOT NULL,
    diff_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_depfp_project ON dependency_fingerprints(project_id, captured_at);
"""


class CallGraphDB:
    def __init__(self, db_path: str) -> None:
        """Store the database path; connection is opened by init()."""
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    @classmethod
    async def create(cls, db_path: str) -> "CallGraphDB":
        """Async factory — create and fully initialize a CallGraphDB instance."""
        obj = cls(db_path)
        await obj.init()
        return obj

    async def init(self) -> None:
        """Open the SQLite connection, apply DDL, and run schema migrations."""
        self._db = await aiosqlite.connect(self._path, check_same_thread=False)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(DDL)
        await self._check_and_migrate()
        # Create project_id-dependent indexes after migration so they work on
        # both fresh installs and upgraded DBs.
        await self._ensure_indexes()

    async def _check_and_migrate(self) -> None:
        """Detect old single-project schema and upgrade in place."""
        async with self._db.execute("PRAGMA table_info(nodes)") as cur:
            cols = {row[1] for row in await cur.fetchall()}
        if "project_id" not in cols:
            await self._migrate_to_multi_project()
        if "decorators" not in cols:
            await self._db.execute(
                "ALTER TABLE nodes ADD COLUMN decorators TEXT NOT NULL DEFAULT '[]'"
            )
            await self._db.commit()
        if "embedding_model" not in cols:
            await self._db.execute(
                "ALTER TABLE nodes ADD COLUMN embedding_model TEXT NOT NULL DEFAULT ''"
            )
            await self._db.commit()
        if "is_external" not in cols:
            await self._db.execute(
                "ALTER TABLE nodes ADD COLUMN is_external INTEGER NOT NULL DEFAULT 0"
            )
            await self._db.commit()

    async def _ensure_indexes(self) -> None:
        """Idempotent creation of project_id-dependent indexes. Safe after migration."""
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id)"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_edges_project ON edges(project_id)"
        )
        # Replace old unique index (no project_id) with the multi-project version.
        await self._db.execute("DROP INDEX IF EXISTS idx_edges_unique")
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique "
            "ON edges(project_id, caller_id, callee_id, edge_type, file)"
        )
        await self._db.commit()

    async def _migrate_to_multi_project(self) -> None:
        """
        One-time migration from single-project to multi-project schema.
        Existing data is assigned to project_id='default'.
        vec0 embedding tables are dropped and recreated by EmbeddingStore.init()
        because their IDs will change format ({project_id}::{node_id}).
        """
        print("[storage] Migrating schema to multi-project — existing data → project 'default'")

        # Dedup edges before adding the unique index (old rows may have duplicates).
        await self._db.execute(
            "DELETE FROM edges WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM edges GROUP BY caller_id, callee_id, edge_type, file)"
        )

        # Rebuild nodes with composite PK (project_id, id).
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS nodes_new (
                project_id  TEXT NOT NULL DEFAULT 'default',
                id          TEXT NOT NULL,
                file        TEXT NOT NULL,
                module      TEXT NOT NULL,
                type        TEXT NOT NULL,
                name        TEXT NOT NULL,
                signature   TEXT NOT NULL DEFAULT '',
                docstring   TEXT NOT NULL DEFAULT '',
                summary     TEXT NOT NULL DEFAULT '',
                body_hash   TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (project_id, id)
            )
        """)
        await self._db.execute("""
            INSERT OR IGNORE INTO nodes_new
            SELECT 'default', id, file, module, type, name,
                   signature, docstring, summary,
                   COALESCE(body_hash, '')
            FROM nodes
        """)
        await self._db.execute("DROP TABLE nodes")
        await self._db.execute("ALTER TABLE nodes_new RENAME TO nodes")

        # Add project_id to edges.
        try:
            await self._db.execute(
                "ALTER TABLE edges ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'"
            )
        except Exception:
            pass  # already exists (shouldn't happen, but safe)

        # Add project_id to decisions.
        try:
            await self._db.execute(
                "ALTER TABLE decisions ADD COLUMN project_id TEXT NOT NULL DEFAULT 'default'"
            )
        except Exception:
            pass

        # Create projects table and register default project.
        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id           TEXT PRIMARY KEY,
                name         TEXT NOT NULL,
                root         TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL,
                last_indexed TEXT
            )
        """)
        await self._db.execute(
            "INSERT OR IGNORE INTO projects(id, name, root, created_at) "
            "VALUES ('default', 'default', '', datetime('now'))"
        )

        # NOTE: function_embeddings / decision_embeddings (vec0 virtual tables) are NOT
        # dropped here — the vec0 extension is not loaded at this stage. EmbeddingStore.init()
        # loads the extension and detects old-format IDs, wiping them before re-index.

        await self._db.commit()
        print("[storage] Migration to multi-project schema complete.")
        print("[storage] NOTE: Embeddings will be cleared by EmbeddingStore on next startup.")

    async def close(self) -> None:
        """Close the underlying SQLite connection."""
        if self._db:
            await self._db.close()

    # ── Projects ───────────────────────────────────────────────────────────

    async def upsert_project(self, project_id: str, name: str, root: str = "") -> None:
        """Insert or update a project record, refreshing the last_indexed timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO projects(id, name, root, created_at, last_indexed)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, root=excluded.root, last_indexed=excluded.last_indexed""",
            (project_id, name, root, now, now),
        )
        await self._db.commit()

    async def list_projects(self) -> list[dict]:
        """Return all registered projects with node and edge counts."""
        async with self._db.execute(
            """
            SELECT p.id, p.name, p.root, p.created_at, p.last_indexed,
                   COUNT(DISTINCT n.id)           AS node_count,
                   COUNT(DISTINCT e.id)           AS edge_count
            FROM projects p
            LEFT JOIN nodes n ON n.project_id = p.id
            LEFT JOIN edges e ON e.project_id = p.id
            GROUP BY p.id
            ORDER BY p.created_at
            """
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def delete_project(self, project_id: str) -> dict:
        """Delete all data for a project: nodes, edges, decisions, snapshots, violations."""
        import json
        async with self._db.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id = ?", (project_id,)
        ) as cur:
            node_count = (await cur.fetchone())[0]
        async with self._db.execute(
            "SELECT COUNT(*) FROM edges WHERE project_id = ?", (project_id,)
        ) as cur:
            edge_count = (await cur.fetchone())[0]

        await self._db.execute("DELETE FROM nodes WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM edges WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM decisions WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM contract_violations WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM agent_improvements WHERE project_id = ?", (project_id,))
        await self._db.execute("DELETE FROM project_home_snapshots WHERE project_id = ?", (project_id,))

        # Remove project_id from any contracts that reference it.
        async with self._db.execute(
            "SELECT id, project_ids FROM contracts", ()
        ) as cur:
            contracts = await cur.fetchall()
        for row in contracts:
            cid, pids_json = row
            pids = json.loads(pids_json or "[]")
            if project_id in pids:
                pids.remove(project_id)
                await self._db.execute(
                    "UPDATE contracts SET project_ids = ? WHERE id = ?",
                    (json.dumps(pids), cid),
                )

        await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()
        return {"project_id": project_id, "nodes_deleted": node_count, "edges_deleted": edge_count}

    # ── Nodes ──────────────────────────────────────────────────────────────

    async def upsert_nodes(self, nodes: list[FunctionNode], project_id: str) -> None:
        """Insert or update a batch of function/class nodes for a project."""
        import json
        rows = [
            (project_id, n.id, n.file, n.module, n.type, n.name,
             n.signature, n.docstring, n.body_hash, json.dumps(n.decorators),
             1 if n.is_external else 0)
            for n in nodes
        ]
        await self._db.executemany(
            """INSERT INTO nodes(project_id,id,file,module,type,name,signature,docstring,summary,body_hash,decorators,is_external)
               VALUES(?,?,?,?,?,?,?,?,'',?,?,?)
               ON CONFLICT(project_id,id) DO UPDATE SET
                   file=excluded.file, module=excluded.module, type=excluded.type,
                   name=excluded.name, signature=excluded.signature,
                   docstring=excluded.docstring, body_hash=excluded.body_hash,
                   decorators=excluded.decorators, is_external=excluded.is_external""",
            rows,
        )
        await self._db.commit()

    async def update_summary(self, node_id: str, summary: str, project_id: str) -> None:
        """Update the LLM-generated summary for a single node."""
        await self._db.execute(
            "UPDATE nodes SET summary=? WHERE id=? AND project_id=?",
            (summary, node_id, project_id),
        )
        await self._db.commit()

    async def batch_update_summaries(
        self, summaries: dict[str, str], project_id: str
    ) -> None:
        """Bulk-update summaries for multiple nodes in a single executemany call."""
        if not summaries:
            return
        await self._db.executemany(
            "UPDATE nodes SET summary=? WHERE id=? AND project_id=?",
            [(s, nid, project_id) for nid, s in summaries.items()],
        )
        await self._db.commit()

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._db.commit()

    async def update_node_embedding_meta(
        self, node_id: str, summary: str | None, model: str, project_id: str
    ) -> None:
        """Write embedding model (and optionally summary) back to a node row."""
        if summary is not None:
            await self._db.execute(
                "UPDATE nodes SET summary = ?, embedding_model = ? WHERE id = ? AND project_id = ?",
                (summary, model, node_id, project_id),
            )
        else:
            await self._db.execute(
                "UPDATE nodes SET embedding_model = ? WHERE id = ? AND project_id = ?",
                (model, node_id, project_id),
            )

    async def get_nodes_needing_enrichment(
        self, project_id: str, limit: int = 500
    ) -> list[dict]:
        """Return nodes still on the large-model fallback that need LLM enrichment."""
        async with self._db.execute(
            "SELECT id, name, signature, docstring, file FROM nodes "
            "WHERE project_id = ? AND is_external = 0 AND embedding_model = 'text-embedding-3-large' LIMIT ?",
            (project_id, limit),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def count_nodes_by_model(self, project_id: str, model: str) -> int:
        """Count how many nodes in a project use a specific embedding model."""
        async with self._db.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id = ? AND embedding_model = ?",
            (project_id, model),
        ) as cur:
            return (await cur.fetchone())[0]

    async def get_all_nodes(self, project_id: str) -> list[dict]:
        """Return all nodes for a project including summary and docstring fields."""
        async with self._db.execute(
            "SELECT id, name, file, module, type, signature, docstring, summary, body_hash "
            "FROM nodes WHERE project_id = ?",
            (project_id,),
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]

    async def get_project_root(self, project_id: str) -> str:
        """Return the filesystem root path for a project, or empty string if not found."""
        async with self._db.execute(
            "SELECT root FROM projects WHERE id = ?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else ""


    async def get_node(self, node_id: str, project_id: str | None = None) -> dict | None:
        """Fetch a single node by ID, optionally scoped to a project."""
        if project_id:
            async with self._db.execute(
                "SELECT * FROM nodes WHERE id=? AND project_id=?", (node_id, project_id)
            ) as cur:
                row = await cur.fetchone()
        else:
            async with self._db.execute(
                "SELECT * FROM nodes WHERE id=? LIMIT 1", (node_id,)
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None

    async def get_nodes_by_file(self, file_path: str, project_id: str | None = None) -> list[dict]:
        """Return all nodes belonging to a specific source file."""
        if project_id:
            async with self._db.execute(
                "SELECT * FROM nodes WHERE file=? AND project_id=?", (file_path, project_id)
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        async with self._db.execute(
            "SELECT * FROM nodes WHERE file=?", (file_path,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def find_node_by_name(
        self, name: str, project_id: str | None = None
    ) -> list[dict]:
        """Fuzzy match: exact id, then exact name, then suffix match."""
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()

        async with self._db.execute(
            f"SELECT * FROM nodes WHERE (id=? OR name=?){pid_clause}",
            (name, name, *pid_args),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            return rows

        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._db.execute(
            f"SELECT * FROM nodes WHERE id LIKE ? ESCAPE '\\'{pid_clause}",
            (f"%.{escaped}", *pid_args),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Edges ──────────────────────────────────────────────────────────────

    async def upsert_edges(
        self, edges: list[CallEdge], all_node_ids: set[str], project_id: str
    ) -> None:
        """Insert call edges, resolving bare callee names to known node IDs where possible."""
        rows = []
        for e in edges:
            callee_id = _resolve_callee(e.callee_name, all_node_ids)
            rows.append((project_id, e.caller_id, callee_id, e.edge_type, e.file))
        await self._db.executemany(
            "INSERT OR IGNORE INTO edges(project_id,caller_id,callee_id,edge_type,file) "
            "VALUES(?,?,?,?,?)",
            rows,
        )
        await self._db.commit()

    async def delete_file_data(self, file_path: str, project_id: str) -> None:
        """Delete all nodes and edges belonging to a file in a project."""
        await self._db.execute(
            "DELETE FROM nodes WHERE file=? AND project_id=?", (file_path, project_id)
        )
        await self._db.execute(
            "DELETE FROM edges WHERE file=? AND project_id=?", (file_path, project_id)
        )
        await self._db.commit()

    # ── MCP query tools ────────────────────────────────────────────────────

    async def get_callers(
        self, function_name: str, project_id: str | None = None
    ) -> list[dict]:
        """Return all functions that directly call the target function."""
        targets = await self.find_node_by_name(function_name, project_id)
        if not targets:
            return []
        pid_clause = " AND e.project_id=?" if project_id else ""
        seen: set[str] = set()
        results = []
        for t in targets:
            async with self._db.execute(
                f"""
                SELECT n.id, n.name, n.file, n.module, n.signature, n.project_id, n.is_external, e.edge_type
                FROM edges e
                JOIN nodes n ON n.id = e.caller_id AND n.project_id = e.project_id
                WHERE e.callee_id = ?{pid_clause}
                """,
                (t["id"], *((project_id,) if project_id else ())),
            ) as cur:
                for r in await cur.fetchall():
                    key = f"{r['project_id']}::{r['id']}"
                    if key not in seen:
                        seen.add(key)
                        results.append(dict(r))
        return results

    async def get_callees(
        self, function_name: str, project_id: str | None = None
    ) -> list[dict]:
        """Return all functions directly called by the target function."""
        targets = await self.find_node_by_name(function_name, project_id)
        if not targets:
            return []
        pid_clause = " AND e.project_id=?" if project_id else ""
        seen: set[str] = set()
        results = []
        for t in targets:
            async with self._db.execute(
                f"""
                SELECT n.id, n.name, n.file, n.module, n.signature, n.project_id, n.is_external, e.edge_type
                FROM edges e
                JOIN nodes n ON n.id = e.callee_id AND n.project_id = e.project_id
                WHERE e.caller_id = ?{pid_clause}
                """,
                (t["id"], *((project_id,) if project_id else ())),
            ) as cur:
                for r in await cur.fetchall():
                    key = f"{r['project_id']}::{r['id']}"
                    if key not in seen:
                        seen.add(key)
                        results.append(dict(r))
        return results

    async def get_impact_radius(
        self, function_name: str, depth: int = 2, project_id: str | None = None
    ) -> list[dict]:
        """BFS traversal outward from function_name up to `depth` levels."""
        targets = await self.find_node_by_name(function_name, project_id)
        if not targets:
            return []

        pid_clause = " AND project_id=?" if project_id else ""

        visited: dict[str, int] = {}  # node_id -> depth level
        queue: deque[tuple[str, int]] = deque()
        for t in targets:
            visited[t["id"]] = 0
            queue.append((t["id"], 0))

        while queue:
            current_id, level = queue.popleft()
            if level >= depth:
                continue
            async with self._db.execute(
                f"SELECT DISTINCT caller_id FROM edges WHERE callee_id=?{pid_clause}",
                (current_id, *((project_id,) if project_id else ())),
            ) as cur:
                for row in await cur.fetchall():
                    nid = row[0]
                    if nid not in visited:
                        visited[nid] = level + 1
                        queue.append((nid, level + 1))

        if not visited:
            return []
        ph = ",".join("?" * len(visited))
        pid_where = f" AND project_id=?" if project_id else ""
        async with self._db.execute(
            f"SELECT * FROM nodes WHERE id IN ({ph}){pid_where}",
            [*visited.keys(), *((project_id,) if project_id else ())],
        ) as cur:
            rows = {r["id"]: dict(r) for r in await cur.fetchall()}
        results = []
        for nid, lvl in visited.items():
            if nid in rows:
                node = rows[nid]
                node["impact_depth"] = lvl
                results.append(node)
        results.sort(key=lambda x: x["impact_depth"])
        return results

    async def get_all_node_ids(self, project_id: str | None = None) -> set[str]:
        """Return all node IDs (internal + external) for edge resolution."""
        if project_id:
            async with self._db.execute(
                "SELECT id FROM nodes WHERE project_id=?", (project_id,)
            ) as cur:
                return {row[0] for row in await cur.fetchall()}
        async with self._db.execute("SELECT id FROM nodes") as cur:
            return {row[0] for row in await cur.fetchall()}

    async def get_internal_node_ids(self, project_id: str) -> set[str]:
        """Return only project-owned node IDs — excludes external library nodes."""
        async with self._db.execute(
            "SELECT id FROM nodes WHERE project_id = ? AND is_external = 0", (project_id,)
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def list_external_dependencies(self, project_id: str) -> list[dict]:
        """
        Return all external library nodes for a project, grouped by library.
        Includes caller_count — how many internal functions reference each symbol.
        """
        async with self._db.execute(
            """SELECT n.id, n.name, n.module, n.signature,
                      COUNT(DISTINCT e.caller_id) AS caller_count
               FROM nodes n
               LEFT JOIN edges e ON e.callee_id = n.id AND e.project_id = n.project_id
               WHERE n.project_id = ? AND n.is_external = 1
               GROUP BY n.id
               ORDER BY n.module, caller_count DESC""",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()

        from collections import defaultdict
        grouped: dict[str, list] = defaultdict(list)
        for row in rows:
            node_id, name, module, signature, caller_count = row
            library = module.replace("external.", "", 1).split(".")[0]
            grouped[library].append({
                "id": node_id,
                "name": name,
                "signature": signature[:120],
                "caller_count": caller_count,
            })

        return [
            {"library": lib, "symbol_count": len(syms),
             "symbols": sorted(syms, key=lambda s: -s["caller_count"])}
            for lib, syms in sorted(grouped.items())
        ]

    async def save_dependency_fingerprint(
        self,
        project_id: str,
        fp_id: str,
        captured_at: str,
        fingerprint_hash: str,
        snapshot_json: str,
        diff_json: str | None,
    ) -> None:
        await self._db.execute(
            """INSERT INTO dependency_fingerprints
               (id, project_id, captured_at, fingerprint_hash, snapshot_json, diff_json)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (fp_id, project_id, captured_at, fingerprint_hash, snapshot_json, diff_json),
        )
        await self._db.commit()

    async def get_latest_dependency_fingerprint(
        self, project_id: str
    ) -> dict | None:
        async with self._db.execute(
            """SELECT id, project_id, captured_at, fingerprint_hash, snapshot_json, diff_json
               FROM dependency_fingerprints
               WHERE project_id = ?
               ORDER BY captured_at DESC
               LIMIT 1""",
            (project_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def list_dependency_fingerprint_history(
        self, project_id: str, limit: int = 50
    ) -> list[dict]:
        """Summary rows — no snapshot_json to keep response size small."""
        async with self._db.execute(
            """SELECT id, captured_at, fingerprint_hash,
                      json_extract(diff_json, '$.removed_symbols')  AS removed_json,
                      json_extract(diff_json, '$.added_symbols')    AS added_json,
                      json_extract(diff_json, '$.changed_symbols')  AS changed_json,
                      json_extract(diff_json, '$.version_changes')  AS version_json
               FROM dependency_fingerprints
               WHERE project_id = ?
               ORDER BY captured_at DESC
               LIMIT ?""",
            (project_id, limit),
        ) as cur:
            rows = await cur.fetchall()

        import json as _json
        result = []
        for row in rows:
            removed  = _json.loads(row["removed_json"]  or "[]")
            added    = _json.loads(row["added_json"]    or "[]")
            changed  = _json.loads(row["changed_json"]  or "[]")
            versions = _json.loads(row["version_json"]  or "[]")
            entry = {
                "id": row["id"],
                "captured_at": row["captured_at"],
                "fingerprint_hash": row["fingerprint_hash"],
                "removed_count": len(removed),
                "added_count":   len(added),
                "changed_count": len(changed),
                "version_change_count": len(versions),
            }
            if removed:
                entry["removed_symbols"] = [s["id"] for s in removed]
            if changed:
                entry["changed_symbols"] = [s["id"] for s in changed]
            if versions:
                entry["version_changes"] = versions
            result.append(entry)
        return result

    async def get_dependency_fingerprint_by_id(
        self, fingerprint_id: str
    ) -> dict | None:
        async with self._db.execute(
            """SELECT id, project_id, captured_at, fingerprint_hash, snapshot_json, diff_json
               FROM dependency_fingerprints WHERE id = ?""",
            (fingerprint_id,),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def get_nodes_missing_docstring(
        self, project_id: str, exclude_names: set[str] | None = None
    ) -> list[dict]:
        """Return function nodes without a docstring, for PRESENCE-rule contract checking."""
        async with self._db.execute(
            """SELECT id, name FROM nodes
               WHERE project_id = ?
               AND is_external = 0
               AND type NOT IN ('class', 'ClassDef')
               AND (docstring = '' OR docstring IS NULL)""",
            (project_id,),
        ) as cur:
            rows = await cur.fetchall()
        if not exclude_names:
            return [{"id": r[0], "name": r[1]} for r in rows]
        return [
            {"id": r[0], "name": r[1]} for r in rows
            if r[1].split(".")[-1].lower() not in exclude_names
        ]

    async def get_all_caller_ids(self, project_id: str) -> list[str]:
        """Return the distinct set of caller function IDs for a project."""
        async with self._db.execute(
            "SELECT DISTINCT caller_id FROM edges WHERE project_id = ?", (project_id,)
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    async def get_nodes_with_null_content(self, project_id: str) -> list[str]:
        """
        Return IDs of nodes that have been embedded but from empty content —
        no summary and no docstring. Their vectors encode raw code tokens only,
        not semantics. These are candidates for enrich_summaries.
        """
        async with self._db.execute(
            """SELECT id FROM nodes
               WHERE project_id = ?
               AND is_external = 0
               AND embedding_model != ''
               AND summary = ''
               AND docstring = ''
               AND type NOT IN ('class', 'ClassDef')""",
            (project_id,),
        ) as cur:
            return [row[0] for row in await cur.fetchall()]

    # ── Decision helpers ───────────────────────────────────────────────────

    async def insert_decision(self, decision: dict) -> None:
        """Insert a raw decision record into the decisions table."""
        await self._db.execute(
            """INSERT OR REPLACE INTO decisions
               (id,project_id,type,description,rejected_alternatives,
                trigger,parent_decision_id,created_at)
               VALUES(:id,:project_id,:type,:description,:rejected_alternatives,
                      :trigger,:parent_decision_id,:created_at)""",
            decision,
        )
        await self._db.commit()

    async def insert_decision_functions(
        self, decision_id: str, function_ids: list[str]
    ) -> None:
        """Link a decision to a list of function IDs in decision_functions."""
        rows = [(decision_id, fid) for fid in function_ids]
        await self._db.executemany(
            "INSERT OR IGNORE INTO decision_functions(decision_id,function_id) VALUES(?,?)",
            rows,
        )
        await self._db.commit()

    async def get_decisions_for_function(
        self, function_name: str, project_id: str | None = None
    ) -> list[dict]:
        """Return all decisions linked to a function, in chronological order."""
        targets = await self.find_node_by_name(function_name, project_id)
        if not targets:
            return []
        pid_clause = " AND d.project_id=?" if project_id else ""
        seen: set[str] = set()
        results = []
        for t in targets:
            async with self._db.execute(
                f"""
                SELECT d.* FROM decisions d
                JOIN decision_functions df ON df.decision_id = d.id
                WHERE df.function_id = ?{pid_clause}
                ORDER BY d.created_at ASC
                """,
                (t["id"], *((project_id,) if project_id else ())),
            ) as cur:
                for r in await cur.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        results.append(dict(r))
        return results

    # ── Contracts ──────────────────────────────────────────────────────────

    async def create_contract(
        self,
        contract_id: str,
        project_ids: list[str],
        title: str,
        natural_language: str,
        rule_type: str = "SEMANTIC",
        structural_expression: str = "{}",
        threshold: float = 0.85,
    ) -> dict:
        """Persist a new contract record in draft status and return it."""
        import json
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO contracts
               (id, project_ids, title, natural_language, rule_type,
                structural_expression, threshold, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
            (contract_id, json.dumps(project_ids), title, natural_language,
             rule_type, structural_expression, threshold, now),
        )
        await self._db.commit()
        return await self.get_contract(contract_id)

    async def get_contract(self, contract_id: str) -> dict | None:
        """Fetch a contract by ID, decoding project_ids from JSON."""
        import json
        async with self._db.execute(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r["project_ids"] = json.loads(r["project_ids"])
        return r

    async def list_contracts(self, project_id: str | None = None) -> list[dict]:
        """Return all contracts, optionally filtered to those covering a specific project."""
        import json
        async with self._db.execute(
            "SELECT * FROM contracts ORDER BY created_at DESC"
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        result = []
        for r in rows:
            r["project_ids"] = json.loads(r["project_ids"])
            if project_id is None or project_id in r["project_ids"]:
                result.append(r)
        return result

    async def update_contract_status(self, contract_id: str, status: str) -> None:
        """Set the status field on a contract (e.g. 'active' or 'draft')."""
        await self._db.execute(
            "UPDATE contracts SET status = ? WHERE id = ?", (status, contract_id)
        )
        await self._db.commit()

    async def update_contract_structural(
        self, contract_id: str, structural_expression: str
    ) -> None:
        """Replace the structural_expression JSON for a contract in-place."""
        await self._db.execute(
            "UPDATE contracts SET structural_expression = ? WHERE id = ?",
            (structural_expression, contract_id),
        )
        await self._db.commit()

    async def delete_contract(self, contract_id: str) -> None:
        """Hard-delete a contract and its cascading examples and violations."""
        await self._db.execute("DELETE FROM contracts WHERE id = ?", (contract_id,))
        await self._db.commit()

    async def upsert_contract_examples(
        self, contract_id: str, examples: list[dict]
    ) -> None:
        """Replace all examples for a contract. examples: [{type, code}]"""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "DELETE FROM contract_examples WHERE contract_id = ?", (contract_id,)
        )
        import uuid
        rows = [
            (str(uuid.uuid4()), contract_id, ex["type"], ex["code"], now)
            for ex in examples
        ]
        await self._db.executemany(
            "INSERT INTO contract_examples(id, contract_id, example_type, code, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )
        await self._db.commit()

    async def list_contract_examples(self, contract_id: str) -> list[dict]:
        """Return all violation and compliance examples for a contract, ordered by creation time."""
        async with self._db.execute(
            "SELECT * FROM contract_examples WHERE contract_id = ? ORDER BY created_at",
            (contract_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def log_violation(
        self,
        contract_id: str,
        function_id: str,
        project_id: str,
        violation_type: str,
        score: float,
    ) -> None:
        """Record a single contract violation event for a function."""
        import uuid
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO contract_violations
               (id, contract_id, function_id, project_id, violation_type, score, detected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), contract_id, function_id, project_id,
             violation_type, score, now),
        )
        await self._db.commit()

    async def list_violations(
        self, project_id: str | None = None, limit: int = 100
    ) -> list[dict]:
        """Return recent contract violations, optionally filtered by project."""
        if project_id:
            async with self._db.execute(
                """SELECT cv.*, c.title AS contract_title
                   FROM contract_violations cv
                   JOIN contracts c ON c.id = cv.contract_id
                   WHERE cv.project_id = ?
                   ORDER BY cv.detected_at DESC LIMIT ?""",
                (project_id, limit),
            ) as cur:
                return [dict(r) for r in await cur.fetchall()]
        async with self._db.execute(
            """SELECT cv.*, c.title AS contract_title
               FROM contract_violations cv
               JOIN contracts c ON c.id = cv.contract_id
               ORDER BY cv.detected_at DESC LIMIT ?""",
            (limit,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Agent Improvements ─────────────────────────────────────────────────

    async def create_improvement(
        self,
        improvement_id: str,
        project_id: str,
        title: str,
        description: str,
        affected_functions: list[str],
        severity: str,
        suggested_fix: str,
        reproduction_steps: str,
    ) -> dict:
        """Insert a new agent improvement report and return it."""
        import json
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO agent_improvements
               (id, project_id, title, description, affected_functions,
                severity, suggested_fix, reproduction_steps, status, filed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)""",
            (
                improvement_id, project_id, title, description,
                json.dumps(affected_functions), severity,
                suggested_fix, reproduction_steps, now,
            ),
        )
        await self._db.commit()
        return await self.get_improvement(improvement_id)

    async def get_improvement(self, improvement_id: str) -> dict | None:
        """Fetch a single improvement by ID, deserializing affected_functions from JSON."""
        import json
        async with self._db.execute(
            "SELECT * FROM agent_improvements WHERE id = ?", (improvement_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r["affected_functions"] = json.loads(r["affected_functions"])
        return r

    async def list_improvements(
        self,
        project_id: str | None = None,
        status: str | None = "open",
    ) -> list[dict]:
        """List improvement reports, filterable by project and status, newest first."""
        import json
        clauses, params = [], []
        if project_id is not None:
            clauses.append("project_id = ?")
            params.append(project_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._db.execute(
            f"SELECT * FROM agent_improvements {where} ORDER BY filed_at DESC",
            params,
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        for r in rows:
            r["affected_functions"] = json.loads(r["affected_functions"])
        return rows

    async def resolve_improvement(
        self,
        improvement_id: str,
        resolution_notes: str,
        status: str = "done",
    ) -> dict:
        """Mark an improvement as resolved with a final status and resolution notes."""
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """UPDATE agent_improvements
               SET status = ?, resolved_at = ?, resolution_notes = ?
               WHERE id = ?""",
            (status, now, resolution_notes, improvement_id),
        )
        await self._db.commit()
        return await self.get_improvement(improvement_id)

    # ── Project Home snapshots ─────────────────────────────────────────────────

    async def _save_project_snapshot(
        self, project_id: str, hashes: dict[str, str], captured_at: str
    ) -> None:
        """Persist the current function-hash map so the next call can diff against it."""
        await self._db.execute(
            """INSERT INTO project_home_snapshots(project_id, hashes, captured_at)
               VALUES(?, ?, ?)
               ON CONFLICT(project_id) DO UPDATE SET
                   hashes=excluded.hashes, captured_at=excluded.captured_at""",
            (project_id, json.dumps(hashes), captured_at),
        )
        await self._db.commit()

    async def _load_project_snapshot(self, project_id: str) -> dict | None:
        """Load the previous project-home snapshot, or None if this is the first call."""
        async with self._db.execute(
            "SELECT hashes, captured_at FROM project_home_snapshots WHERE project_id = ?",
            (project_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"hashes": json.loads(row[0]), "captured_at": row[1]}

    # ── Project Home ───────────────────────────────────────────────────────

    async def fetch_graph_data(self, project_id: str) -> GraphData:
        """Fetch all raw graph data for one project — no analysis, pure SQL."""
        db = self._db

        async with db.execute(
            """SELECT id, name, type, module, summary, docstring, body_hash, decorators
               FROM nodes WHERE project_id = ? AND is_external = 0""",
            (project_id,),
        ) as cur:
            nodes = [dict(r) for r in await cur.fetchall()]

        async with db.execute(
            """SELECT callee_id, COUNT(DISTINCT caller_id) AS cnt
               FROM edges WHERE project_id = ?
               GROUP BY callee_id""",
            (project_id,),
        ) as cur:
            caller_counts: dict[str, int] = {r[0]: r[1] for r in await cur.fetchall()}

        async with db.execute(
            "SELECT caller_id, callee_id FROM edges WHERE project_id = ?",
            (project_id,),
        ) as cur:
            edges = list(await cur.fetchall())

        async with db.execute(
            """SELECT df.function_id, COUNT(*) AS cnt
               FROM decision_functions df
               JOIN decisions d ON d.id = df.decision_id
               WHERE d.project_id = ?
               GROUP BY df.function_id""",
            (project_id,),
        ) as cur:
            churn: dict[str, int] = {r[0]: r[1] for r in await cur.fetchall()}

        async with db.execute(
            """SELECT d.id, d.type, d.description, d.created_at,
                      GROUP_CONCAT(df.function_id) AS function_ids
               FROM decisions d
               LEFT JOIN decision_functions df ON df.decision_id = d.id
               WHERE d.project_id = ?
               GROUP BY d.id
               ORDER BY d.created_at DESC LIMIT 5""",
            (project_id,),
        ) as cur:
            recent_decisions = []
            for r in await cur.fetchall():
                rd = dict(r)
                rd["function_ids"] = rd["function_ids"].split(",") if rd["function_ids"] else []
                rd["description"] = rd["description"][:120]
                recent_decisions.append(rd)

        async with db.execute(
            "SELECT id, title, status, project_ids FROM contracts WHERE status = 'active'"
        ) as cur:
            all_contracts = [dict(r) for r in await cur.fetchall()]
        contracts = [
            c for c in all_contracts
            if project_id in json.loads(c.get("project_ids") or "[]")
        ]

        async with db.execute(
            """SELECT COUNT(*) FROM contract_violations cv
               JOIN contracts c ON c.id = cv.contract_id
               WHERE cv.project_id = ?
               AND cv.detected_at > datetime('now', '-7 days')""",
            (project_id,),
        ) as cur:
            recent_violation_count: int = (await cur.fetchone())[0]

        prev_snapshot = await self._load_project_snapshot(project_id)
        current_hashes = {n["id"]: n.get("body_hash", "") for n in nodes}

        decisions_since: list[dict] = []
        if prev_snapshot:
            prev_time = prev_snapshot.get("captured_at", "")
            async with db.execute(
                """SELECT id, type, description, created_at FROM decisions
                   WHERE project_id = ? AND created_at > ?
                   ORDER BY created_at DESC LIMIT 20""",
                (project_id, prev_time),
            ) as cur:
                for r in await cur.fetchall():
                    d = dict(r)
                    d["description"] = d["description"][:120]
                    decisions_since.append(d)

        return GraphData(
            project_id=project_id,
            nodes=nodes,
            edges=edges,
            caller_counts=caller_counts,
            churn=churn,
            contracts=contracts,
            recent_violation_count=recent_violation_count,
            recent_decisions=recent_decisions,
            prev_snapshot=prev_snapshot,
            current_hashes=current_hashes,
            decisions_since=decisions_since,
        )

    async def get_project_home_data(self, project_id: str) -> dict:
        """
        Compute a full architectural intelligence snapshot for one project.
        All SQL — no LLM calls. Used by get_project_home MCP tool and web UI.
        """
        import dataclasses
        from ..analysis import ArchitectureAnalyzer

        data = await self.fetch_graph_data(project_id)
        snapshot = ArchitectureAnalyzer().snapshot(data)
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._save_project_snapshot(project_id, data.current_hashes, now_iso)
        return dataclasses.asdict(snapshot)


def _resolve_callee(callee_name: str, all_ids: set[str]) -> str:
    """Best-effort resolve bare callee name to a known node id."""
    if callee_name in all_ids:
        return callee_name
    suffix = f".{callee_name}"
    matches = [nid for nid in all_ids if nid.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return callee_name
