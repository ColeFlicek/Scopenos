from __future__ import annotations

import asyncio
import json
import os
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import asyncpg

from .models import GraphData
from .parser import CallEdge, FunctionNode


def _pg(sql: str) -> str:
    """Convert SQLite ? placeholders to PostgreSQL $1, $2, ... positional params."""
    n = 0
    def _sub(_m):
        nonlocal n
        n += 1
        return f"${n}"
    return re.sub(r"\?", _sub, sql)


class _Cursor:
    """Minimal cursor-like wrapper so asyncpg results look like aiosqlite cursors."""

    def __init__(self, rows, description=None):
        self._rows = rows
        self.description = description or []

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        pass


class _DB:
    """Asyncpg pool wrapper with an aiosqlite-compatible interface.

    Allows CallGraphDB methods to use the same execute/fetchall/fetchone
    patterns they used with aiosqlite, without rewriting every query site.
    """

    def __init__(self, pool: asyncpg.Pool) -> None:
        self._pool = pool

    def execute(self, sql: str, params=()):
        """Return an async context manager that runs a query and exposes results."""
        return _QueryContext(self._pool, _pg(sql), params)

    async def executemany(self, sql: str, data) -> None:
        if not data:
            return
        async with self._pool.acquire() as conn:
            await conn.executemany(_pg(sql), data)

    async def commit(self) -> None:
        pass  # asyncpg auto-commits in non-transaction context

    async def close(self) -> None:
        await self._pool.close()


class _QueryContext:
    """Returned by _DB.execute(). Supports both usage patterns:

    await db.execute(sql, params)                       — fire-and-forget
    async with db.execute(sql, params) as cur: ...      — fetch results
    """

    def __init__(self, pool, sql, params):
        self._pool = pool
        self._sql = sql
        self._params = params

    def __await__(self):
        return self._run().__await__()

    async def _run(self) -> None:
        async with self._pool.acquire() as conn:
            await conn.execute(self._sql, *self._params)

    async def __aenter__(self) -> _Cursor:
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(self._sql, *self._params)
        description = [(k,) for k in rows[0].keys()] if rows else []
        return _Cursor(rows, description)

    async def __aexit__(self, *_):
        pass


# Schema is in schema.sql — applied at init() via asyncpg.


_DEFAULT_DSN = "postgresql://phronosis:phronosis@localhost/phronosis"
_SCHEMA_SQL = Path(__file__).parent.parent.parent / "schema.sql"


class CallGraphDB:
    def __init__(self, dsn: str) -> None:
        """Store the DSN; connection pool is opened by init()."""
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._db: _DB | None = None
        # In-memory cache for get_project_home_data: project_id → (monotonic_ts, result)
        self._project_home_cache: dict[str, tuple[float, dict]] = {}

    @classmethod
    async def create(cls, dsn: str = "") -> "CallGraphDB":
        """Async factory — create and fully initialize a CallGraphDB instance."""
        resolved = dsn or os.getenv("DATABASE_URL", _DEFAULT_DSN)
        obj = cls(resolved)
        await obj.init()
        return obj

    async def init(self) -> None:
        """Open the asyncpg connection pool and apply schema."""
        from pgvector.asyncpg import register_vector

        # Bootstrap the extension before the pool so register_vector succeeds
        # on each connection's init callback (it does a type lookup on vector).
        bootstrap = await asyncpg.connect(self._dsn)
        try:
            await bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector")
        finally:
            await bootstrap.close()

        async def _init_conn(conn: asyncpg.Connection) -> None:
            await register_vector(conn)

        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=2, max_size=10, init=_init_conn
        )
        self._db = _DB(self._pool)
        schema = _SCHEMA_SQL.read_text()
        async with self._pool.acquire() as conn:
            await conn.execute(schema)

    async def close(self) -> None:
        """Close the connection pool."""
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

    async def rename_project(self, project_id: str, new_name: str) -> bool:
        """Update the display name of a project. Returns False if project not found."""
        async with self._db.execute(
            "UPDATE projects SET name = ? WHERE id = ? RETURNING id",
            (new_name, project_id),
        ) as cur:
            row = await cur.fetchone()
        return row is not None

    async def list_user_projects(self, user_id: str) -> list[dict]:
        """Return all projects accessible to user_id, including demo projects."""
        async with self._db.execute(
            """
            SELECT p.id, p.name, p.root, p.last_indexed, pa.role,
                   COUNT(DISTINCT n.id) AS node_count
            FROM project_access pa
            JOIN projects p ON p.id = pa.project_id
            LEFT JOIN nodes n ON n.project_id = p.id
            WHERE pa.user_id = ?
            GROUP BY p.id, pa.role
            ORDER BY p.last_indexed DESC
            """,
            (user_id,),
        ) as cur:
            private = [dict(r) for r in await cur.fetchall()]

        async with self._db.execute(
            """
            SELECT p.id, p.name, '' AS root, p.last_indexed, 'viewer' AS role,
                   COUNT(DISTINCT n.id) AS node_count
            FROM demo_projects dp
            JOIN projects p ON p.id = dp.project_id
            LEFT JOIN nodes n ON n.project_id = p.id
            GROUP BY p.id
            ORDER BY p.last_indexed DESC
            """,
        ) as cur:
            demos = [dict(r) for r in await cur.fetchall()]

        return private + demos

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
        self, project_id: str, limit: int = 500, force: bool = False
    ) -> list[dict]:
        """Return nodes on the large-model fallback that need LLM summarisation.

        Normally skips functions that already have a summary — calling enrich_summaries
        twice on the same project should be a no-op. Pass force=True to include functions
        that already have summaries (e.g. docstring was updated and the old Claude summary
        is now stale).
        """
        summary_clause = "" if force else " AND (summary IS NULL OR summary = '')"
        async with self._db.execute(
            f"SELECT id, name, signature, docstring, file, summary FROM nodes "
            f"WHERE project_id = ? AND is_external = 0"
            f" AND embedding_model = 'text-embedding-3-large'{summary_clause} LIMIT ?",
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
            "INSERT INTO edges(project_id,caller_id,callee_id,edge_type,file) "
            "VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING",
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
               GROUP BY n.id, n.name, n.module, n.signature
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
            """SELECT id, captured_at, fingerprint_hash, diff_json
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
            diff = _json.loads(row["diff_json"] or "{}") if row["diff_json"] else {}
            removed  = diff.get("removed_symbols", [])
            added    = diff.get("added_symbols", [])
            changed  = diff.get("changed_symbols", [])
            versions = diff.get("version_changes", [])
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

    async def get_library_dependents(
        self, library_name: str, project_id: str
    ) -> list[dict]:
        """
        Return all internal functions that call any symbol in the given library.
        Answers: "if library X changes, which of my functions are exposed?"
        """
        async with self._db.execute(
            """SELECT DISTINCT n.id, n.name, n.file, n.module, n.signature,
                      COUNT(e.callee_id) AS call_count
               FROM edges e
               JOIN nodes n ON n.id = e.caller_id AND n.project_id = e.project_id
               WHERE e.callee_id LIKE ?
                 AND e.project_id = ?
                 AND n.is_external = 0
               GROUP BY n.id, n.name, n.file, n.module, n.signature
               ORDER BY call_count DESC, n.module, n.name""",
            (f"external.{library_name}.%", project_id),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

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
            """INSERT INTO decisions
               (id,project_id,type,description,rejected_alternatives,
                trigger,parent_decision_id,created_at)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   description=excluded.description,
                   rejected_alternatives=excluded.rejected_alternatives,
                   trigger=excluded.trigger""",
            (
                decision["id"], decision["project_id"], decision["type"],
                decision["description"], decision["rejected_alternatives"],
                decision["trigger"], decision.get("parent_decision_id"),
                decision["created_at"],
            ),
        )
        await self._db.commit()

    async def insert_decision_functions(
        self, decision_id: str, function_ids: list[str]
    ) -> None:
        """Link a decision to a list of function IDs in decision_functions."""
        rows = [(decision_id, fid) for fid in function_ids]
        await self._db.executemany(
            "INSERT INTO decision_functions(decision_id,function_id) VALUES(?,?) ON CONFLICT DO NOTHING",
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
                      STRING_AGG(df.function_id, ',') AS function_ids
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
               AND cv.detected_at::TIMESTAMPTZ > NOW() - INTERVAL '7 days'""",
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

    async def get_project_home_data(
        self, project_id: str, max_age_seconds: int = 0
    ) -> dict:
        """
        Compute a full architectural intelligence snapshot for one project.
        All SQL — no LLM calls. Used by get_project_home MCP tool and web UI.

        max_age_seconds: if > 0 and a cached result is younger than this many
        seconds, return the cache without re-running the 8 SQL queries,
        ArchitectureAnalyzer, and snapshot write. The hook passes 1800 (its
        gate TTL); the MCP tool passes 300. 0 always recomputes.
        """
        import dataclasses
        import time
        from ..analysis import ArchitectureAnalyzer

        if max_age_seconds > 0:
            cached = self._project_home_cache.get(project_id)
            if cached and (time.monotonic() - cached[0]) < max_age_seconds:
                return cached[1]

        data = await self.fetch_graph_data(project_id)
        snapshot = ArchitectureAnalyzer().snapshot(data)
        result = dataclasses.asdict(snapshot)
        now_iso = datetime.now(timezone.utc).isoformat()
        await self._save_project_snapshot(project_id, data.current_hashes, now_iso)

        import time as _time
        self._project_home_cache[project_id] = (_time.monotonic(), result)
        return result


    # ── Auth ───────────────────────────────────────────────────────────────

    async def create_user(self, email: str, plan: str = "free") -> dict:
        """Create a new user and return the user record."""
        import uuid
        user_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO users (id, email, plan, created_at) VALUES (?, ?, ?, ?)",
            (user_id, email, plan, now),
        )
        await self._db.commit()
        return {"id": user_id, "email": email, "plan": plan, "created_at": now}

    async def create_api_key(self, user_id: str, name: str = "") -> str:
        """Create an API key for a user. Returns the raw key once — never stored."""
        import hashlib
        import secrets
        import uuid
        raw_key = secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "INSERT INTO api_keys (id, user_id, key_hash, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, key_hash, name, now),
        )
        await self._db.commit()
        return raw_key

    async def get_user_by_key(self, raw_key: str) -> dict | None:
        """Look up the user for a raw API key. Returns user dict or None."""
        import hashlib
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        async with self._db.execute(
            """SELECT u.id, u.email, u.plan, u.created_at
               FROM api_keys k JOIN users u ON u.id = k.user_id
               WHERE k.key_hash = ?""",
            (key_hash,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        await self._db.execute(
            "UPDATE api_keys SET last_used = ? WHERE key_hash = ?",
            (now, key_hash),
        )
        await self._db.commit()
        return dict(row)

    async def check_project_access(
        self, user_id: str, project_id: str, operation: str
    ) -> bool:
        """Return True if user_id may perform operation on project_id.

        Demo projects: any authenticated user may read; write is denied.
        Private projects: owner role allows read and write; viewer allows read only.
        """
        async with self._db.execute(
            "SELECT 1 FROM demo_projects WHERE project_id = ?", (project_id,)
        ) as cur:
            is_demo = await cur.fetchone() is not None

        if is_demo:
            return operation == "read"

        async with self._db.execute(
            "SELECT role FROM project_access WHERE user_id = ? AND project_id = ?",
            (user_id, project_id),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return False
        role = row["role"]
        if role == "owner":
            return True
        if role == "viewer":
            return operation == "read"
        return False


def _resolve_callee(callee_name: str, all_ids: set[str]) -> str:
    """Best-effort resolve bare callee name to a known node id."""
    if callee_name in all_ids:
        return callee_name
    suffix = f".{callee_name}"
    matches = [nid for nid in all_ids if nid.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return callee_name
