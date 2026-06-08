from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
from typing import Any

import aiosqlite

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
    body_hash   TEXT NOT NULL DEFAULT '',
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
        if self._db:
            await self._db.close()

    # ── Projects ───────────────────────────────────────────────────────────

    async def upsert_project(self, project_id: str, name: str, root: str = "") -> None:
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

    # ── Nodes ──────────────────────────────────────────────────────────────

    async def upsert_nodes(self, nodes: list[FunctionNode], project_id: str) -> None:
        rows = [
            (project_id, n.id, n.file, n.module, n.type, n.name,
             n.signature, n.docstring, n.body_hash)
            for n in nodes
        ]
        await self._db.executemany(
            """INSERT INTO nodes(project_id,id,file,module,type,name,signature,docstring,summary,body_hash)
               VALUES(?,?,?,?,?,?,?,?,'',?)
               ON CONFLICT(project_id,id) DO UPDATE SET
                   file=excluded.file, module=excluded.module, type=excluded.type,
                   name=excluded.name, signature=excluded.signature,
                   docstring=excluded.docstring, body_hash=excluded.body_hash""",
            rows,
        )
        await self._db.commit()

    async def update_summary(self, node_id: str, summary: str, project_id: str) -> None:
        await self._db.execute(
            "UPDATE nodes SET summary=? WHERE id=? AND project_id=?",
            (summary, node_id, project_id),
        )
        await self._db.commit()

    async def batch_update_summaries(
        self, summaries: dict[str, str], project_id: str
    ) -> None:
        if not summaries:
            return
        await self._db.executemany(
            "UPDATE nodes SET summary=? WHERE id=? AND project_id=?",
            [(s, nid, project_id) for nid, s in summaries.items()],
        )
        await self._db.commit()

    async def get_node(self, node_id: str, project_id: str | None = None) -> dict | None:
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
        targets = await self.find_node_by_name(function_name, project_id)
        if not targets:
            return []
        pid_clause = " AND e.project_id=?" if project_id else ""
        seen: set[str] = set()
        results = []
        for t in targets:
            async with self._db.execute(
                f"""
                SELECT n.id, n.name, n.file, n.module, n.signature, n.project_id, e.edge_type
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
        targets = await self.find_node_by_name(function_name, project_id)
        if not targets:
            return []
        pid_clause = " AND e.project_id=?" if project_id else ""
        seen: set[str] = set()
        results = []
        for t in targets:
            async with self._db.execute(
                f"""
                SELECT n.id, n.name, n.file, n.module, n.signature, n.project_id, e.edge_type
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
        if project_id:
            async with self._db.execute(
                "SELECT id FROM nodes WHERE project_id=?", (project_id,)
            ) as cur:
                return {row[0] for row in await cur.fetchall()}
        async with self._db.execute("SELECT id FROM nodes") as cur:
            return {row[0] for row in await cur.fetchall()}

    # ── Decision helpers ───────────────────────────────────────────────────

    async def insert_decision(self, decision: dict) -> None:
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
        rows = [(decision_id, fid) for fid in function_ids]
        await self._db.executemany(
            "INSERT OR IGNORE INTO decision_functions(decision_id,function_id) VALUES(?,?)",
            rows,
        )
        await self._db.commit()

    async def get_decisions_for_function(
        self, function_name: str, project_id: str | None = None
    ) -> list[dict]:
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


def _resolve_callee(callee_name: str, all_ids: set[str]) -> str:
    """Best-effort resolve bare callee name to a known node id."""
    if callee_name in all_ids:
        return callee_name
    suffix = f".{callee_name}"
    matches = [nid for nid in all_ids if nid.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return callee_name
