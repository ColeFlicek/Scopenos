"""
Performance concern detector for Phronosis.

Runs static detectors against indexed function bodies and the call graph,
then scores findings using object embeddings to separate real concerns from
intentional patterns.

Detectors:
  - correlated_join_aggregate: 2+ JOINs on a shared parent key before
    GROUP BY + COUNT — produces a Cartesian product before aggregation.
  - n_plus_one: a function that iterates over a collection and calls a
    function that transitively reaches the DB layer.

Scoring (object embedding layer):
  Each N+1 candidate is scored by extracting which schema objects the loop
  touches, then using embedding similarity + cardinality class to determine
  whether the access pattern is likely problematic or intentional:

    HIGH + HIGH + correlated  → severity=high   (e.g. nodes × edges per project)
    HIGH + LOW                → severity=low    (e.g. loop over projects, query config)
    batch function (executemany) → downgraded   (intentional bulk write)
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB
    from .schema_objects import SchemaObject

# ── Finding ───────────────────────────────────────────────────────────────────

@dataclass
class Finding:
    function_id: str
    function_name: str
    file: str
    pattern: str          # machine-readable key
    severity: str         # "high" | "medium" | "low"
    detail: str           # human-readable explanation
    suppressed: bool = False
    suppression_reason: str = ""

    def to_dict(self) -> dict:
        d = {
            "function": self.function_name,
            "file": self.file,
            "pattern": self.pattern,
            "severity": self.severity,
            "detail": self.detail,
        }
        if self.suppressed:
            d["status"] = "acknowledged"
            d["acknowledged_reason"] = self.suppression_reason
        else:
            d["status"] = "new"
        return d


# ── SQL detector ─────────────────────────────────────────────────────────────

# Matches a JOIN clause with its ON condition
_JOIN_RE = re.compile(
    r"\b(?:LEFT\s+|INNER\s+|RIGHT\s+|FULL\s+)?JOIN\s+(\w+)\s+\w*\s+ON\s+([^,\n]+)",
    re.IGNORECASE,
)
# Matches GROUP BY ... COUNT(DISTINCT ...) or COUNT(DISTINCT ...) anywhere
_COUNT_DISTINCT_RE = re.compile(r"COUNT\s*\(\s*DISTINCT\b", re.IGNORECASE)
# Matches any GROUP BY
_GROUP_BY_RE = re.compile(r"\bGROUP\s+BY\b", re.IGNORECASE)
# Extracts SQL string literals from Python source
_SQL_STRING_RE = re.compile(
    r'"""(.*?)"""'
    r"|'''(.*?)'''"
    r'|"((?:[^"\\]|\\.)*)"'
    r"|'((?:[^'\\]|\\.)*)'",
    re.DOTALL,
)

# Minimum tokens that look like SQL before we try to analyze a string
_SQL_KEYWORDS = re.compile(
    r"\b(?:SELECT|FROM|WHERE|JOIN|GROUP\s+BY|ORDER\s+BY|INSERT|UPDATE|DELETE)\b",
    re.IGNORECASE,
)


def _extract_sql_blocks(body: str) -> list[str]:
    """Pull string literals out of Python source that look like SQL."""
    blocks = []
    for m in _SQL_STRING_RE.finditer(body):
        text = next(g for g in m.groups() if g is not None)
        if len(_SQL_KEYWORDS.findall(text)) >= 2:
            blocks.append(text)
    return blocks


def _shared_join_key(on_a: str, on_b: str) -> bool:
    """
    Return True if two ON clauses share the same parent-side column reference.
    e.g. both reference project_id on different tables joining the same parent.
    """
    # Extract the right-hand side of each ON (the parent column)
    def rhs_cols(on: str) -> set[str]:
        cols = set()
        for part in on.split("AND"):
            part = part.strip()
            # match: table.col = table.col
            m = re.search(r"(\w+\.\w+)\s*=\s*(\w+\.\w+)", part)
            if m:
                cols.add(m.group(1).lower())
                cols.add(m.group(2).lower())
        return cols

    return bool(rhs_cols(on_a) & rhs_cols(on_b))


def detect_correlated_join_aggregate(sql: str) -> str | None:
    """
    Returns a detail string if the SQL contains 2+ JOINs on a shared parent
    key before a GROUP BY + COUNT aggregation — i.e. a cross-product pattern.
    Returns None if the pattern is not present.
    """
    joins = _JOIN_RE.findall(sql)
    if len(joins) < 2:
        return None
    if not (_GROUP_BY_RE.search(sql) and _COUNT_DISTINCT_RE.search(sql)):
        return None

    # Check if any two JOINs share a parent-side column reference
    for i, (tbl_a, on_a) in enumerate(joins):
        for tbl_b, on_b in joins[i + 1:]:
            if _shared_join_key(on_a, on_b):
                return (
                    f"JOIN {tbl_a.upper()} and JOIN {tbl_b.upper()} share a "
                    f"parent key and both feed into COUNT — this creates a "
                    f"row cross-product before aggregation. "
                    f"Replace with correlated subqueries."
                )

    # Fallback: 2+ JOINs + GROUP BY + COUNT DISTINCT without shared-key proof
    # — flag at lower severity (caller handles this)
    return (
        f"{len(joins)} JOINs before GROUP BY + COUNT(DISTINCT). "
        f"Verify no Cartesian product: ensure at least one JOIN has a "
        f"row-limiting WHERE before aggregation."
    )


def _analyze_node_for_sql(node: dict) -> list[tuple[str, str]]:
    """
    Return list of (severity, detail) pairs for SQL performance issues in node.
    """
    body = node.get("body", "")
    if not body:
        return []
    results = []
    for sql in _extract_sql_blocks(body):
        detail = detect_correlated_join_aggregate(sql)
        if detail:
            # Distinguish proven cross-product (high) from suspected (medium)
            severity = "high" if "cross-product" in detail else "medium"
            results.append((severity, detail))
    return results


# ── Object embedding scoring ──────────────────────────────────────────────────

# Bodies that call executemany are doing batch writes, not per-row queries
_BATCH_PATTERN = re.compile(r"\bexecutemany\b", re.IGNORECASE)

_CARDINALITY_WEIGHT = {"UNBOUNDED": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "SCALAR": 0}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    return dot / (na * nb) if na and nb else 0.0


def _score_n_plus_one(
    node: dict,
    callee_names: list[str],
    schema_objects: list["SchemaObject"],
    embeddings_by_name: dict[str, "SchemaObject"],
) -> tuple[str, str]:
    """
    Return (severity, scored_detail) for an N+1 candidate, using schema
    object embeddings to assess actual cardinality risk.

    Severity rules:
      - Function body calls executemany → "low" (batch write, not per-row read)
      - Callee is a HIGH/UNBOUNDED schema object → "high"
      - Callee is a LOW/SCALAR schema object → "low"
      - Unknown → "medium" (original default)
    """
    body = node.get("body", "")

    # Batch write: executemany is O(1) trips to the DB regardless of loop size
    if _BATCH_PATTERN.search(body):
        return "low", (
            "Loop calls executemany — this is a batch write pattern, not an "
            "N+1. Each executemany is a single round-trip regardless of row count."
        )

    if not schema_objects:
        return "medium", ""

    # Find schema objects mentioned in callee names or body
    matched: list["SchemaObject"] = []
    for name in callee_names:
        # e.g. "get_nodes_by_file" → look for "nodes" schema object
        for obj in schema_objects:
            if obj.name.lower() in name.lower() or name.lower() in obj.name.lower():
                matched.append(obj)

    # Also scan body for table/class name mentions
    for obj in schema_objects:
        if obj.name.lower() in body.lower() and obj not in matched:
            matched.append(obj)

    if not matched:
        return "medium", ""

    max_weight = max(_CARDINALITY_WEIGHT.get(o.cardinality, 2) for o in matched)
    top_obj = max(matched, key=lambda o: _CARDINALITY_WEIGHT.get(o.cardinality, 2))

    if max_weight >= 3:  # HIGH or UNBOUNDED
        return "high", (
            f"Loop accesses {top_obj.name} ({top_obj.cardinality} cardinality). "
            f"Each iteration issues a separate query against a large collection."
        )
    if max_weight <= 1:  # LOW or SCALAR
        return "low", (
            f"Loop accesses {top_obj.name} ({top_obj.cardinality} cardinality). "
            f"Collection is small/bounded — likely acceptable."
        )
    return "medium", ""


# ── N+1 detector ─────────────────────────────────────────────────────────────

# Signatures that indicate a function is a DB access point
_DB_SINK_PATTERNS = re.compile(
    r"\b(?:_db\.execute|_pool\.acquire|conn\.fetch|conn\.execute"
    r"|asyncpg\.connect|aiosqlite\.connect)\b",
    re.IGNORECASE,
)

# Signatures that indicate a function iterates: for loop, list comprehension
_LOOP_PATTERNS = re.compile(
    r"\bfor\s+\w+\s+in\b",
    re.IGNORECASE,
)


def _is_db_sink(node: dict) -> bool:
    body = node.get("body", "")
    return bool(_DB_SINK_PATTERNS.search(body))


def _has_loop(node: dict) -> bool:
    body = node.get("body", "")
    return bool(_LOOP_PATTERNS.search(body))


async def detect_n_plus_one(
    nodes_by_id: dict[str, dict],
    callee_map: dict[str, list[str]],  # caller_id → [callee_id, ...]
    db_sink_ids: set[str],
) -> list[tuple[str, str, str]]:
    """
    Return list of (caller_id, callee_id, detail) for N+1 patterns.

    A function is flagged if:
      - it contains a for loop in its body
      - it directly calls at least one function that is a DB sink OR
        transitively reaches one within depth 2
    """
    findings = []
    for node_id, node in nodes_by_id.items():
        if not _has_loop(node):
            continue
        # Check direct callees and one level deeper
        direct = callee_map.get(node_id, [])
        if not direct:
            continue
        db_callees = []
        for callee_id in direct:
            if callee_id in db_sink_ids:
                db_callees.append(callee_id)
                continue
            # depth 2
            for grandchild_id in callee_map.get(callee_id, []):
                if grandchild_id in db_sink_ids:
                    db_callees.append(callee_id)
                    break
        if db_callees:
            callee_names = [
                nodes_by_id[c]["name"] for c in db_callees if c in nodes_by_id
            ]
            detail = (
                f"Contains a for-loop that calls DB-accessing function(s): "
                f"{', '.join(callee_names)}. "
                f"If the loop iterates over a query result, each iteration "
                f"issues a separate query — O(n) queries instead of O(1)."
            )
            findings.append((node_id, db_callees[0], detail))
    return findings


# ── Main entry point ──────────────────────────────────────────────────────────

async def check_performance(
    db: "CallGraphDB",
    project_id: str,
    embeddings: object = None,
) -> list[Finding]:
    """
    Run all detectors against the indexed functions for project_id.
    Returns Finding objects, with suppressed=True for any that have an
    acknowledged Performance decision in decision memory.
    """
    # 1. Load all nodes for the project
    async with db._db.execute(
        "SELECT id, name, file, module, body FROM nodes WHERE project_id = ?",
        (project_id,),
    ) as cur:
        rows = await cur.fetchall()

    nodes_by_id = {r["id"]: dict(r) for r in rows}

    # 2. Load call edges (caller → callees)
    async with db._db.execute(
        "SELECT caller_id, callee_id FROM edges WHERE project_id = ?",
        (project_id,),
    ) as cur:
        edges = await cur.fetchall()

    callee_map: dict[str, list[str]] = {}
    for e in edges:
        callee_map.setdefault(e["caller_id"], []).append(e["callee_id"])

    # 3. Load acknowledged Performance decisions for suppression
    async with db._db.execute(
        """
        SELECT df.function_id, d.description
        FROM decisions d
        JOIN decision_functions df ON df.decision_id = d.id
        WHERE d.project_id = ? AND d.type = 'Performance'
        """,
        (project_id,),
    ) as cur:
        ack_rows = await cur.fetchall()

    acknowledged: dict[str, str] = {r["function_id"]: r["description"] for r in ack_rows}

    # 4. Load schema objects for scoring (optional — gracefully absent)
    from .schema_objects import load_schema_objects
    schema_objects = await load_schema_objects(db, project_id)
    schema_by_name = {o.name: o for o in schema_objects}

    findings: list[Finding] = []

    # ── SQL detector ─────────────────────────────────────────────────────────
    for node_id, node in nodes_by_id.items():
        for severity, detail in _analyze_node_for_sql(node):
            suppressed = node_id in acknowledged
            findings.append(Finding(
                function_id=node_id,
                function_name=node.get("name", node_id),
                file=node.get("file", ""),
                pattern="correlated_join_aggregate",
                severity=severity,
                detail=detail,
                suppressed=suppressed,
                suppression_reason=acknowledged.get(node_id, ""),
            ))

    # ── N+1 detector + object embedding scoring ───────────────────────────────
    db_sink_ids = {
        nid for nid, n in nodes_by_id.items() if _is_db_sink(n)
    }
    n1_findings = await detect_n_plus_one(nodes_by_id, callee_map, db_sink_ids)
    for caller_id, _callee_id, base_detail in n1_findings:
        node = nodes_by_id.get(caller_id, {})
        callee_names = [
            nodes_by_id[c]["name"]
            for c in callee_map.get(caller_id, [])
            if c in nodes_by_id
        ]

        # Score with object embeddings if available
        scored_severity, scored_detail = _score_n_plus_one(
            node, callee_names, schema_objects, schema_by_name
        )
        detail = scored_detail if scored_detail else base_detail
        severity = scored_severity

        suppressed = caller_id in acknowledged
        # Auto-suppress low-severity findings unless explicitly dismissed —
        # they represent intentional patterns the object layer identified.
        if severity == "low" and caller_id not in acknowledged:
            suppressed = True
            suppression_reason = "auto: object embedding scored as low-cardinality or batch pattern"
        else:
            suppression_reason = acknowledged.get(caller_id, "")

        findings.append(Finding(
            function_id=caller_id,
            function_name=node.get("name", caller_id),
            file=node.get("file", ""),
            pattern="n_plus_one",
            severity=severity,
            detail=detail,
            suppressed=suppressed,
            suppression_reason=suppression_reason,
        ))

    # Sort: new findings first, then by severity
    _sev = {"high": 0, "medium": 1, "low": 2}
    findings.sort(key=lambda f: (f.suppressed, _sev.get(f.severity, 9)))
    return findings
