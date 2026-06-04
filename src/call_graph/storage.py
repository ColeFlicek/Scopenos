from __future__ import annotations

import asyncio
from collections import defaultdict, deque
from typing import Any

import aiosqlite

from .parser import CallEdge, FunctionNode

DDL = """
CREATE TABLE IF NOT EXISTS nodes (
    id          TEXT PRIMARY KEY,
    file        TEXT NOT NULL,
    module      TEXT NOT NULL,
    type        TEXT NOT NULL,
    name        TEXT NOT NULL,
    signature   TEXT NOT NULL DEFAULT '',
    docstring   TEXT NOT NULL DEFAULT '',
    summary     TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS edges (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id   TEXT NOT NULL,
    callee_id   TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    file        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_caller  ON edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee  ON edges(callee_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file    ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name    ON nodes(name);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edges_unique ON edges(caller_id, callee_id, edge_type, file);

CREATE TABLE IF NOT EXISTS decisions (
    id                   TEXT PRIMARY KEY,
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
"""


class CallGraphDB:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    @classmethod
    async def create(cls, db_path: str) -> "CallGraphDB":
        obj = cls(db_path)
        await obj.init()
        return obj

    async def init(self) -> None:
        self._db = await aiosqlite.connect(self._path, check_same_thread=False)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(DDL)
        # Dedup any duplicate edges from before the UNIQUE index was added.
        # Runs after DDL so the table is guaranteed to exist.
        await self._db.execute(
            "DELETE FROM edges WHERE rowid NOT IN "
            "(SELECT MIN(rowid) FROM edges GROUP BY caller_id, callee_id, edge_type, file)"
        )
        await self._db.commit()

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    # ── Nodes ──────────────────────────────────────────────────────────────

    async def upsert_nodes(self, nodes: list[FunctionNode]) -> None:
        rows = [
            (n.id, n.file, n.module, n.type, n.name, n.signature, n.docstring)
            for n in nodes
        ]
        await self._db.executemany(
            """INSERT INTO nodes(id,file,module,type,name,signature,docstring,summary)
               VALUES(?,?,?,?,?,?,?,'')
               ON CONFLICT(id) DO UPDATE SET
                   file=excluded.file, module=excluded.module, type=excluded.type,
                   name=excluded.name, signature=excluded.signature,
                   docstring=excluded.docstring""",
            rows,
        )
        await self._db.commit()

    async def update_summary(self, node_id: str, summary: str) -> None:
        await self._db.execute("UPDATE nodes SET summary=? WHERE id=?", (summary, node_id))
        await self._db.commit()

    async def get_node(self, node_id: str) -> dict | None:
        async with self._db.execute("SELECT * FROM nodes WHERE id=?", (node_id,)) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None

    async def get_nodes_by_file(self, file_path: str) -> list[dict]:
        async with self._db.execute("SELECT * FROM nodes WHERE file=?", (file_path,)) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def find_node_by_name(self, name: str) -> list[dict]:
        """Fuzzy match: exact id, then exact name, then suffix match."""
        async with self._db.execute("SELECT * FROM nodes WHERE id=? OR name=?", (name, name)) as cur:
            rows = [dict(r) for r in await cur.fetchall()]
        if rows:
            return rows
        # Escape LIKE special chars so underscores/percents in function names are literal.
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._db.execute(
            "SELECT * FROM nodes WHERE id LIKE ? ESCAPE '\\'", (f"%.{escaped}",)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    # ── Edges ──────────────────────────────────────────────────────────────

    async def upsert_edges(self, edges: list[CallEdge], all_node_ids: set[str]) -> None:
        """Insert edges, resolving callee_name to a node id where possible."""
        rows = []
        for e in edges:
            callee_id = _resolve_callee(e.callee_name, all_node_ids)
            rows.append((e.caller_id, callee_id, e.edge_type, e.file))
        await self._db.executemany(
            "INSERT OR IGNORE INTO edges(caller_id,callee_id,edge_type,file) VALUES(?,?,?,?)", rows
        )
        await self._db.commit()

    async def delete_file_data(self, file_path: str) -> None:
        await self._db.execute("DELETE FROM nodes WHERE file=?", (file_path,))
        await self._db.execute("DELETE FROM edges WHERE file=?", (file_path,))
        await self._db.commit()

    # ── MCP query tools ────────────────────────────────────────────────────

    async def get_callers(self, function_name: str) -> list[dict]:
        targets = await self.find_node_by_name(function_name)
        if not targets:
            return []
        results = []
        for t in targets:
            async with self._db.execute(
                """
                SELECT n.id, n.name, n.file, n.module, n.signature, e.edge_type
                FROM edges e
                JOIN nodes n ON n.id = e.caller_id
                WHERE e.callee_id = ?
                """,
                (t["id"],),
            ) as cur:
                results.extend(dict(r) for r in await cur.fetchall())
        return results

    async def get_callees(self, function_name: str) -> list[dict]:
        targets = await self.find_node_by_name(function_name)
        if not targets:
            return []
        results = []
        for t in targets:
            async with self._db.execute(
                """
                SELECT n.id, n.name, n.file, n.module, n.signature, e.edge_type
                FROM edges e
                JOIN nodes n ON n.id = e.callee_id
                WHERE e.caller_id = ?
                """,
                (t["id"],),
            ) as cur:
                results.extend(dict(r) for r in await cur.fetchall())
        return results

    async def get_impact_radius(self, function_name: str, depth: int = 2) -> list[dict]:
        """BFS traversal outward from function_name up to `depth` levels."""
        targets = await self.find_node_by_name(function_name)
        if not targets:
            return []

        visited: dict[str, int] = {}  # node_id -> depth level
        queue: deque[tuple[str, int]] = deque()

        for t in targets:
            visited[t["id"]] = 0
            queue.append((t["id"], 0))

        while queue:
            current_id, level = queue.popleft()
            if level >= depth:
                continue
            # Outward = callers of current node (things that will break if we change it)
            async with self._db.execute(
                "SELECT DISTINCT caller_id FROM edges WHERE callee_id=?", (current_id,)
            ) as cur:
                for row in await cur.fetchall():
                    nid = row[0]
                    if nid not in visited:
                        visited[nid] = level + 1
                        queue.append((nid, level + 1))

        if not visited:
            return []
        ph = ",".join("?" * len(visited))
        async with self._db.execute(
            f"SELECT * FROM nodes WHERE id IN ({ph})", list(visited.keys())
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

    # ── Decision helpers ───────────────────────────────────────────────────

    async def insert_decision(self, decision: dict) -> None:
        await self._db.execute(
            """INSERT OR REPLACE INTO decisions
               (id,type,description,rejected_alternatives,trigger,parent_decision_id,created_at)
               VALUES(:id,:type,:description,:rejected_alternatives,:trigger,:parent_decision_id,:created_at)""",
            decision,
        )
        await self._db.commit()

    async def insert_decision_functions(self, decision_id: str, function_ids: list[str]) -> None:
        rows = [(decision_id, fid) for fid in function_ids]
        await self._db.executemany(
            "INSERT OR IGNORE INTO decision_functions(decision_id,function_id) VALUES(?,?)", rows
        )
        await self._db.commit()

    async def get_decisions_for_function(self, function_name: str) -> list[dict]:
        targets = await self.find_node_by_name(function_name)
        if not targets:
            return []
        seen: set[str] = set()
        results = []
        for t in targets:
            async with self._db.execute(
                """
                SELECT d.* FROM decisions d
                JOIN decision_functions df ON df.decision_id = d.id
                WHERE df.function_id = ?
                ORDER BY d.created_at ASC
                """,
                (t["id"],),
            ) as cur:
                for r in await cur.fetchall():
                    if r["id"] not in seen:
                        seen.add(r["id"])
                        results.append(dict(r))
        return results

    async def get_all_node_ids(self) -> set[str]:
        async with self._db.execute("SELECT id FROM nodes") as cur:
            return {row[0] for row in await cur.fetchall()}


def _resolve_callee(callee_name: str, all_ids: set[str]) -> str:
    """Best-effort resolve bare callee name to a known node id."""
    # Exact match first
    if callee_name in all_ids:
        return callee_name
    # Suffix match: find any id ending with .callee_name
    suffix = f".{callee_name}"
    matches = [nid for nid in all_ids if nid.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    # Fall back to storing the bare name (unresolved)
    return callee_name
