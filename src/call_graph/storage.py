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
from ..branch_tracking import classify_conflicts, empty_conflict_result


# Columns returned to callers (and ultimately to LLM tool responses).
# body and body_hash are excluded: body is large source code the LLM should
# read via file reads, not receive in navigation responses; body_hash is an
# internal integrity field with no meaning to an agent.
_NODE_COLS = (
    "id, project_id, file, module, type, name, signature, "
    "docstring, summary, decorators, start_line, end_line, "
    "is_async, structural_layer, is_external, embedding_model"
)


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
        self._idx = 0
        self.description = description or []

    async def fetchall(self):
        return self._rows

    async def fetchone(self):
        return self._rows[0] if self._rows else None

    def __aiter__(self):
        self._idx = 0
        return self

    async def __anext__(self):
        if self._idx >= len(self._rows):
            raise StopAsyncIteration
        row = self._rows[self._idx]
        self._idx += 1
        return row

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


# Org database schema applied at init() via asyncpg.
# schema_org.sql creates public-schema org tables and the create_project_schema()
# / drop_project_schema() PL/pgSQL functions. Per-project schemas are created
# on demand by CallGraphDB.create_project_schema().

_DEFAULT_DSN = "postgresql://scopenos:scopenos@localhost/scopenos"
_SCHEMA_SQL = Path(__file__).parent.parent.parent / "schema_org.sql"


def derive_schema_name(project_id: str) -> str:
    """Derive a valid PostgreSQL schema name from a project slug.

    Rules:
    - Replace all non-alphanumeric characters with underscores
    - Ensure the name starts with a letter (prefix 'p' if it starts with a digit)
    - Truncate to 63 characters (Postgres identifier limit)
    - Result is always lowercase
    """
    import re
    slug = re.sub(r"[^a-zA-Z0-9]", "_", project_id).lower()
    if slug and slug[0].isdigit():
        slug = "p" + slug
    return slug[:63] or "project"


class CallGraphDB:
    def __init__(self, dsn: str, schema: str = "") -> None:
        """Store the DSN and optional project schema; connection pool opened by init().

        schema: the Postgres schema for per-project tables (e.g. 'django').
                When set, all connections use SET search_path TO "{schema}", public
                so unqualified table names resolve to the project schema first,
                then fall back to public for org-level tables.
                Leave empty to use the default search_path (public only).
        """
        self._dsn = dsn
        self._schema = schema
        self._pool: asyncpg.Pool | None = None
        self._db: _DB | None = None
        self._project_dbs: dict[str, "CallGraphDB"] = {}

    @classmethod
    async def create(cls, dsn: str = "", schema: str = "") -> "CallGraphDB":
        """Async factory — create and fully initialize a CallGraphDB instance."""
        resolved = dsn or os.getenv("DATABASE_URL", _DEFAULT_DSN)
        obj = cls(resolved, schema=schema)
        await obj.init()
        return obj

    async def init(self) -> None:
        """Open the asyncpg connection pool and apply schema."""
        from pgvector.asyncpg import register_vector

        # Bootstrap the extension before the pool so register_vector succeeds
        # on each connection's init callback (it does a type lookup on vector).
        # Silently ignores InsufficientPrivilegeError — the extension is pre-created
        # by the provisioner (superuser) and may already exist.
        bootstrap = await asyncpg.connect(self._dsn)
        try:
            await bootstrap.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except asyncpg.exceptions.InsufficientPrivilegeError:
            pass  # extension already created by provisioner; non-superuser can't re-create
        finally:
            await bootstrap.close()

        _schema = self._schema

        async def _init_conn(conn: asyncpg.Connection) -> None:
            await register_vector(conn)

        async def _setup_conn(conn: asyncpg.Connection) -> None:
            if _schema:
                # Re-applied on every acquire because asyncpg resets session state
                # (including search_path) when a connection is released to the pool.
                # search_path routes per-project table reads/writes to the project
                # schema first, falling back to public for org-level tables.
                await conn.execute(f'SET search_path TO "{_schema}", public')

        self._pool = await asyncpg.create_pool(
            self._dsn,
            min_size=2,
            max_size=10,
            init=_init_conn,
            setup=_setup_conn,
            # Recycle idle connections every 5 minutes so stale connections
            # after a Postgres restart are replaced rather than hanging indefinitely.
            max_inactive_connection_lifetime=300.0,
            # Per-query timeout so a hung query never blocks indefinitely.
            command_timeout=30.0,
        )
        self._db = _DB(self._pool)
        schema_sql = _SCHEMA_SQL.read_text()
        async with self._pool.acquire() as conn:
            await conn.execute(schema_sql)

    async def close(self) -> None:
        """Close the connection pool and all cached project-scoped pools."""
        for pdb in list(self._project_dbs.values()):
            if pdb._db:
                await pdb._db.close()
        self._project_dbs.clear()
        if self._db:
            await self._db.close()

    async def project_db(self, schema_name: str) -> "CallGraphDB":
        """Return a project-scoped CallGraphDB whose pool has search_path set to schema_name.

        Pools are cached by schema_name on this instance so repeated calls for
        the same project reuse the same pool without reconnecting. The schema
        must already exist — call create_project_schema() first.
        """
        if schema_name in self._project_dbs:
            return self._project_dbs[schema_name]

        from pgvector.asyncpg import register_vector as _rv
        _s = schema_name

        async def _init_conn(conn: asyncpg.Connection) -> None:
            await _rv(conn)

        async def _setup_conn(conn: asyncpg.Connection) -> None:
            await conn.execute(f'SET search_path TO "{_s}", public')

        pool = await asyncpg.create_pool(
            self._dsn,
            min_size=1,
            max_size=5,
            init=_init_conn,
            setup=_setup_conn,
            max_inactive_connection_lifetime=300.0,
            command_timeout=30.0,
        )
        db = CallGraphDB.__new__(CallGraphDB)
        db._dsn = self._dsn
        db._schema = schema_name
        db._pool = pool
        db._db = _DB(pool)
        db._project_dbs = {}   # project-scoped DBs don't nest
        self._project_dbs[schema_name] = db
        return db

    async def create_project_schema(self, schema_name: str) -> None:
        """Create the project schema if it doesn't already exist (idempotent)."""
        await self._db.execute("SELECT create_project_schema(?)", (schema_name,))

    async def get_schema_name_for_project(self, project_id: str) -> str:
        """Look up the stored schema_name for a project, falling back to derive_schema_name."""
        async with self._db.execute(
            "SELECT schema_name FROM projects WHERE id = ?", (project_id,)
        ) as cur:
            row = await cur.fetchone()
        if row and row["schema_name"]:
            return str(row["schema_name"])
        return derive_schema_name(project_id)

    async def _project_scoped(self, project_id: str) -> "CallGraphDB":
        """Return a project-scoped CallGraphDB for the given project_id.

        If this instance already has a schema set (it IS a project-scoped DB),
        return self — no re-routing needed. Otherwise look up (or derive) the
        project schema name and return a cached project_db instance.

        This is the standard way for read methods to transparently route to the
        correct schema without the caller needing to know about schema routing.
        """
        if self._schema:
            return self
        schema_name = await self.get_schema_name_for_project(project_id)
        return await self.project_db(schema_name)

    # ── Projects ───────────────────────────────────────────────────────────

    async def upsert_project(
        self,
        project_id: str,
        name: str,
        root: str = "",
        branch: str = "",
        head_commit: str = "",
        schema_name: str = "",
        node_count: int | None = None,
        edge_count: int | None = None,
    ) -> None:
        """Insert or update a project record, refreshing the last_indexed timestamp."""
        now = datetime.now(timezone.utc).isoformat()
        sname = schema_name or self._schema or derive_schema_name(project_id)
        count_clause = ""
        count_params: tuple = ()
        if node_count is not None and edge_count is not None:
            count_clause = ", node_count=excluded.node_count, edge_count=excluded.edge_count"
            count_params = (node_count, edge_count)
        await self._db.execute(
            f"""INSERT INTO projects(id, name, root, branch, head_commit, schema_name, created_at, last_indexed{', node_count, edge_count' if count_params else ''})
               VALUES(?, ?, ?, ?, ?, ?, ?, ?{', ?, ?' if count_params else ''})
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, root=excluded.root,
                   branch=excluded.branch, head_commit=excluded.head_commit,
                   schema_name=excluded.schema_name,
                   last_indexed=excluded.last_indexed{count_clause}""",
            (project_id, name, root, branch, head_commit, sname, now, now, *count_params),
        )
        await self._db.commit()
        # Ensure the project schema exists (idempotent — IF NOT EXISTS inside function)
        await self._db.execute("SELECT create_project_schema(?)", (sname,))

    async def list_projects(self) -> list[dict]:
        """Return all registered projects with node and edge counts."""
        async with self._db.execute(
            """
            SELECT id, name, root, branch, head_commit, schema_name,
                   is_fork, parent_schema, created_at, last_indexed,
                   node_count, edge_count
            FROM projects
            ORDER BY created_at
            """
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def compare_projects(self, project_id_a: str, project_id_b: str) -> dict:
        """Diff two project indexes at the call-graph level.

        Returns functions added (in B, not A), removed (in A, not B),
        and changed (same ID, different body_hash). Useful for branch comparison.
        """
        async with self._db.execute(
            "SELECT id, body_hash, name, file, signature FROM nodes "
            "WHERE project_id = ? AND is_external = 0", (project_id_a,)
        ) as cur:
            a_nodes = {r[0]: dict(r) for r in await cur.fetchall()}

        async with self._db.execute(
            "SELECT id, body_hash, name, file, signature FROM nodes "
            "WHERE project_id = ? AND is_external = 0", (project_id_b,)
        ) as cur:
            b_nodes = {r[0]: dict(r) for r in await cur.fetchall()}

        added = [b_nodes[k] for k in b_nodes if k not in a_nodes]
        removed = [a_nodes[k] for k in a_nodes if k not in b_nodes]
        changed = [
            {"id": k, "from": a_nodes[k], "to": b_nodes[k]}
            for k in a_nodes
            if k in b_nodes and a_nodes[k]["body_hash"] != b_nodes[k]["body_hash"]
        ]
        return {
            "project_a": project_id_a,
            "project_b": project_id_b,
            "added": added,
            "removed": removed,
            "changed": changed,
            "summary": {
                "added": len(added),
                "removed": len(removed),
                "changed": len(changed),
            },
        }

    # ── Branch conflict detection ──────────────────────────────────────────────

    async def record_branch_changes(
        self,
        project_id: str,
        branch: str,
        function_ids: list[str],
        head_commit: str = "",
    ) -> None:
        """Log which functions were modified on a branch. Upserts per (project, branch, fn)."""
        if not branch or not function_ids:
            return
        now = datetime.now(timezone.utc).isoformat()
        await self._db.executemany(
            """INSERT INTO branch_function_changes(project_id, branch, function_id, head_commit, modified_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(project_id, branch, function_id) DO UPDATE SET
                   head_commit=excluded.head_commit, modified_at=excluded.modified_at""",
            [(project_id, branch, fn_id, head_commit, now) for fn_id in function_ids],
        )
        await self._db.commit()

    async def get_branch_conflicts(
        self,
        project_id: str,
        function_ids: list[str],
        current_branch: str = "",
    ) -> dict:
        """Find other branches that touched the same functions the caller is working on.

        Returns conflicts grouped by function, showing which competing branches modified
        each function and when. Classification (main_drift, branch grouping) is handled
        by branch_tracking.classify_conflicts so the domain logic is testable without a DB.
        """
        if not function_ids:
            return empty_conflict_result()

        placeholders = ", ".join("?" * len(function_ids))
        params: list = [project_id, *function_ids]
        if current_branch:
            params.append(current_branch)

        async with self._db.execute(
            f"""SELECT branch, function_id, head_commit, modified_at
                FROM branch_function_changes
                WHERE project_id = ?
                  AND function_id IN ({placeholders})
                  {"AND branch != ?" if current_branch else ""}
                ORDER BY modified_at DESC""",
            tuple(params),
        ) as cur:
            rows = [dict(r) for r in await cur.fetchall()]

        return classify_conflicts(rows)

    async def record_commit_changes(
        self,
        project_id: str,
        commit_hash: str,
        function_ids: list[str],
        branch: str = "",
        changed_at: str = "",
    ) -> None:
        """Log which functions changed in a specific commit. Append-only — preserves history."""
        if not commit_hash or not function_ids:
            return
        ts = changed_at or datetime.now(timezone.utc).isoformat()
        await self._db.executemany(
            """INSERT INTO commit_function_changes
                   (project_id, commit_hash, function_id, branch, changed_at)
               VALUES(?, ?, ?, ?, ?)
               ON CONFLICT(project_id, commit_hash, function_id) DO NOTHING""",
            [(project_id, commit_hash, fn_id, branch, ts) for fn_id in function_ids],
        )
        await self._db.commit()

    async def get_co_change_functions(
        self,
        function_id: str,
        project_id: str,
        min_count: int = 3,
        limit: int = 10,
    ) -> list[dict]:
        """Find functions that frequently change in the same commits as function_id.

        Queries commit_function_changes for functions that appear in the same commit
        as function_id at least min_count times. Returns [] when the table is empty
        or function_id has no commit history — callers must handle this gracefully.

        Returns list of {function_id, co_change_count} ordered by count descending.
        """
        async with self._db.execute(
            """SELECT other.function_id, COUNT(*) AS co_change_count
               FROM commit_function_changes mine
               JOIN commit_function_changes other
                 ON other.project_id = mine.project_id
                AND other.commit_hash = mine.commit_hash
                AND other.function_id != mine.function_id
               WHERE mine.project_id = ?
                 AND mine.function_id = ?
               GROUP BY other.function_id
               HAVING COUNT(*) >= ?
               ORDER BY co_change_count DESC
               LIMIT ?""",
            (project_id, function_id, min_count, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

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
                   p.schema_name, p.node_count, p.edge_count
            FROM project_access pa
            JOIN projects p ON p.id = pa.project_id
            WHERE pa.user_id = ?
            ORDER BY p.last_indexed DESC
            """,
            (user_id,),
        ) as cur:
            private = [dict(r) for r in await cur.fetchall()]

        async with self._db.execute(
            """
            SELECT p.id, p.name, '' AS root, p.last_indexed, 'viewer' AS role,
                   p.schema_name, p.node_count, p.edge_count
            FROM demo_projects dp
            JOIN projects p ON p.id = dp.project_id
            ORDER BY p.last_indexed DESC
            """,
        ) as cur:
            demos = [dict(r) for r in await cur.fetchall()]

        return private + demos

    async def delete_project(self, project_id: str) -> dict:
        """Delete a project by dropping its schema and removing the projects row.

        Uses drop_project_schema() SQL function which does DROP SCHEMA CASCADE,
        removing all per-project tables atomically. Contracts referencing this
        project are updated to remove it from their project_ids list.
        """
        import json

        # Capture counts before deletion for the response
        async with self._db.execute(
            "SELECT schema_name, node_count, edge_count FROM projects WHERE id = ?",
            (project_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return {"project_id": project_id, "nodes_deleted": 0, "edges_deleted": 0}
        schema_name, node_count, edge_count = row["schema_name"], row["node_count"], row["edge_count"]

        # Remove project from any contracts that reference it
        async with self._db.execute("SELECT id, project_ids FROM contracts") as cur:
            contracts = await cur.fetchall()
        for crow in contracts:
            cid, pids_json = crow
            pids = json.loads(pids_json or "[]")
            if project_id in pids:
                pids.remove(project_id)
                await self._db.execute(
                    "UPDATE contracts SET project_ids = ? WHERE id = ?",
                    (json.dumps(pids), cid),
                )

        # Drop the project schema (CASCADE removes all per-project tables) and
        # the projects row. drop_project_schema() is a SQL function in schema_org.sql.
        if schema_name:
            await self._db.execute("SELECT drop_project_schema(?)", (schema_name,))
        else:
            await self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        await self._db.commit()
        return {"project_id": project_id, "nodes_deleted": node_count, "edges_deleted": edge_count}

    async def create_project_schema(self, schema_name: str) -> None:
        """Create a new per-project schema with all required tables.

        Calls the create_project_schema() SQL function defined in schema_org.sql.
        Safe to call multiple times (uses CREATE SCHEMA IF NOT EXISTS internally).
        """
        await self._db.execute("SELECT create_project_schema(?)", (schema_name,))
        await self._db.commit()

    async def fork_schema(
        self,
        parent_schema: str,
        fork_schema_name: str,
        skip_tables: list[str] | None = None,
    ) -> None:
        """Create a fork schema by copying structural tables from the parent.

        Tables copied: nodes, edges, function_embeddings, module_patterns,
            commit_function_changes, branch_function_changes, dependency_fingerprints,
            project_home_snapshots, schema_object_embeddings, agent_improvements.

        Tables NOT copied (fork starts empty):
            decisions, decision_embeddings, decision_functions,
            contracts, contract_violations, contract_examples.
        """
        _skip = set(skip_tables or [
            "decisions", "decision_embeddings", "decision_functions",
        ])

        _copy_tables = [
            "nodes", "edges", "function_embeddings", "module_patterns",
            "commit_function_changes", "branch_function_changes",
            "dependency_fingerprints", "project_home_snapshots",
            "schema_object_embeddings", "agent_improvements",
        ]

        async with self._pool.acquire() as conn:
            # Create the fork schema with all empty tables
            await conn.execute("SELECT create_project_schema($1)", fork_schema_name)

            # Copy structural tables from parent, skipping generated columns
            # (e.g. tsv tsvector GENERATED ALWAYS AS ... STORED — Postgres rejects
            # explicit inserts into them even from a SELECT *)
            for table in _copy_tables:
                if table in _skip:
                    continue
                col_rows = await conn.fetch(
                    """SELECT column_name
                       FROM information_schema.columns
                       WHERE table_schema = $1 AND table_name = $2
                         AND is_generated = 'NEVER'
                       ORDER BY ordinal_position""",
                    parent_schema, table,
                )
                if col_rows:
                    cols = ", ".join(f'"{r["column_name"]}"' for r in col_rows)
                    await conn.execute(
                        f'INSERT INTO "{fork_schema_name}".{table} ({cols}) '
                        f'SELECT {cols} FROM "{parent_schema}".{table}'
                    )
                else:
                    await conn.execute(
                        f'INSERT INTO "{fork_schema_name}".{table} '
                        f'SELECT * FROM "{parent_schema}".{table}'
                    )
        await self._db.commit()

    # ── Fork schema helpers (pool access isolated here) ────────────────────

    async def get_node_hashes_in_schema(
        self, schema_name: str, file_paths: list[str]
    ) -> dict[str, str]:
        """Return {node_id: body_hash} for nodes in the given files within schema_name."""
        if not file_paths:
            return {}
        async with self._pool.acquire() as conn:
            rows = await conn.fetch(
                f'SELECT id, body_hash FROM "{schema_name}".nodes WHERE file = ANY($1)',
                file_paths,
            )
        return {str(r["id"]): str(r["body_hash"]) for r in rows}

    async def upsert_nodes_into_schema(
        self, schema_name: str, rows: list[tuple], project_id: str
    ) -> None:
        """Bulk-upsert node rows (already serialized) into a named schema's nodes table."""
        if not rows:
            return
        async with self._pool.acquire() as conn:
            await conn.executemany(
                f"""INSERT INTO "{schema_name}".nodes
                       (project_id,id,file,module,type,name,signature,docstring,summary,
                        body,body_hash,decorators,is_external,start_line,end_line,
                        return_type,is_async,parameter_names,enclosing_class,structural_layer)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,'',$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                    ON CONFLICT(project_id,id) DO UPDATE SET
                        file=excluded.file, module=excluded.module, type=excluded.type,
                        name=excluded.name, signature=excluded.signature,
                        docstring=excluded.docstring, body=excluded.body,
                        body_hash=excluded.body_hash,
                        decorators=excluded.decorators, is_external=excluded.is_external,
                        start_line=excluded.start_line, end_line=excluded.end_line,
                        return_type=excluded.return_type, is_async=excluded.is_async,
                        parameter_names=excluded.parameter_names,
                        enclosing_class=excluded.enclosing_class,
                        structural_layer=excluded.structural_layer""",
                rows,
            )

    async def delete_nodes_from_schema_by_ids(
        self, schema_name: str, node_ids: list[str]
    ) -> None:
        """Delete nodes by ID list from a named schema's nodes table."""
        if not node_ids:
            return
        async with self._pool.acquire() as conn:
            await conn.execute(
                f'DELETE FROM "{schema_name}".nodes WHERE id = ANY($1)',
                node_ids,
            )

    async def insert_fork_project(
        self,
        fork_project_id: str,
        name: str,
        root: str,
        head_commit: str,
        fork_schema_name: str,
        parent_schema: str,
        now: str,
    ) -> None:
        """Insert a fork project row into public.projects with is_fork=TRUE."""
        async with self._pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO projects
                       (id, name, root, branch, head_commit, schema_name,
                        created_at, last_indexed, is_fork, parent_schema)
                   VALUES($1, $2, $3, $4, $5, $6, $7, $8, TRUE, $9)
                   ON CONFLICT(id) DO UPDATE SET
                       name=excluded.name, root=excluded.root,
                       head_commit=excluded.head_commit,
                       schema_name=excluded.schema_name,
                       last_indexed=excluded.last_indexed,
                       is_fork=TRUE,
                       parent_schema=excluded.parent_schema""",
                fork_project_id, name, root, "", head_commit,
                fork_schema_name, now, now, parent_schema,
            )

    async def get_project_is_fork(self, project_id: str) -> bool | None:
        """Return is_fork for the given project, or None if project not found."""
        async with self._pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_fork FROM projects WHERE id = $1", project_id
            )
        if row is None:
            return None
        return bool(row["is_fork"])

    # ── Nodes ──────────────────────────────────────────────────────────────

    async def upsert_nodes(self, nodes: list[FunctionNode], project_id: str) -> None:
        """Insert or update a batch of function/class nodes for a project."""
        import json
        rows = [
            (project_id, n.id, n.file, n.module, n.type, n.name,
             n.signature, n.docstring, n.body, n.body_hash, json.dumps(n.decorators),
             1 if n.is_external else 0, n.start_line, n.end_line,
             n.return_type, 1 if n.is_async else 0,
             json.dumps(n.parameter_names), n.enclosing_class, n.structural_layer)
            for n in nodes
        ]
        await self._db.executemany(
            """INSERT INTO nodes(project_id,id,file,module,type,name,signature,docstring,summary,body,body_hash,decorators,is_external,start_line,end_line,return_type,is_async,parameter_names,enclosing_class,structural_layer)
               VALUES(?,?,?,?,?,?,?,?,'',?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(project_id,id) DO UPDATE SET
                   file=excluded.file, module=excluded.module, type=excluded.type,
                   name=excluded.name, signature=excluded.signature,
                   docstring=excluded.docstring, body=excluded.body,
                   body_hash=excluded.body_hash,
                   decorators=excluded.decorators, is_external=excluded.is_external,
                   start_line=excluded.start_line, end_line=excluded.end_line,
                   return_type=excluded.return_type, is_async=excluded.is_async,
                   parameter_names=excluded.parameter_names,
                   enclosing_class=excluded.enclosing_class,
                   structural_layer=excluded.structural_layer""",
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
            f"SELECT {_NODE_COLS} FROM nodes WHERE file=?", (file_path,)
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_class_siblings(
        self, node_id: str, project_id: str | None = None
    ) -> list[dict]:
        """Return all methods on the same class as node_id.

        Infers the class prefix from the node_id by dropping the last component
        (e.g. 'django.db.models.lookups.Lookup.__eq__' → prefix 'django.db.models.lookups.Lookup.').
        Returns all nodes whose id starts with that prefix.
        """
        parts = node_id.split(".")
        if len(parts) < 2:
            return []
        class_prefix = ".".join(parts[:-1]) + "."
        escaped = class_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT {_NODE_COLS} FROM nodes WHERE id LIKE ? ESCAPE '\\'{pid_clause}",
            (escaped + "%", *pid_args),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def find_node_by_name(
        self, name: str, project_id: str | None = None
    ) -> list[dict]:
        """Match nodes by name, returning all candidates across two lookup strategies.

        Step 1 — exact id or exact name match.
        Step 2 — suffix match (id LIKE '%.{name}'), excluding ids already found.

        Both steps always run. Results from step 1 come first so callers that
        take [0] get the most-specific match. The previous short-circuit
        ('if rows: return rows') meant that when a bare name like 'index_project'
        matched an exact-name node, the suffix search was skipped and a method
        named 'Indexer.index_project' (whose id ends in '.index_project') was
        silently omitted from get_callers / get_callees / get_impact_radius results.
        """
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()

        # Step 1: exact id or exact name
        async with self._db.execute(
            f"SELECT {_NODE_COLS} FROM nodes WHERE (id=? OR name=?){pid_clause}",
            (name, name, *pid_args),
        ) as cur:
            exact = [dict(r) for r in await cur.fetchall()]

        # Step 2: suffix match — always run, skip ids already found in step 1
        seen_ids = {r["id"] for r in exact}
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        async with self._db.execute(
            f"SELECT {_NODE_COLS} FROM nodes WHERE id LIKE ? ESCAPE '\\'{pid_clause}",
            (f"%.{escaped}", *pid_args),
        ) as cur:
            suffix = [dict(r) for r in await cur.fetchall() if r["id"] not in seen_ids]

        return exact + suffix

    async def find_dispatch_handlers(
        self,
        project_id: str,
        verb: str,
    ) -> list[dict]:
        """Return all methods matching the named-dispatch pattern _{verb}_{TypeName}.

        Used for Visitor pattern detection: finds every handler across all
        visitor classes for a given dispatch verb (e.g. 'print' → _print_Sin,
        _print_Cos, etc.) so callers can build the visitor × element matrix.
        """
        # Escape LIKE special chars in verb; wrap with leading/trailing _...%
        escaped_verb = verb.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"\\_{escaped_verb}\\_%"
        async with self._db.execute(
            "SELECT id, name, file FROM nodes "
            "WHERE project_id = ? AND name LIKE ? ESCAPE '\\' AND is_external = 0",
            (project_id, pattern),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_class_methods(
        self, class_id: str, project_id: str | None = None
    ) -> list[dict]:
        """Return all method/function nodes directly under class_id.

        Includes the body column (truncated at index time to 2000 chars) so that
        pattern detectors can check for raise NotImplementedError in addition to
        the @abstractmethod decorator.
        """
        escaped = class_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT {_NODE_COLS}, body FROM nodes "
            f"WHERE id LIKE ? ESCAPE '\\' AND type IN ('method','function') "
            f"AND is_external=0{pid_clause}",
            (f"{escaped}.%", *pid_args),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def find_base_classes(
        self, class_id: str, project_id: str | None = None
    ) -> list[str]:
        """Return fully-qualified ids of classes that class_id directly inherits from.

        Resolves bare callee names (e.g. 'ABC') to qualified ids via find_node_by_name
        when the indexer could not resolve them at index time.
        """
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT callee_id FROM edges "
            f"WHERE caller_id=? AND edge_type='inherits'{pid_clause}",
            (class_id, *pid_args),
        ) as cur:
            raw = [r["callee_id"] for r in await cur.fetchall()]

        resolved = []
        for rid in raw:
            if "." in rid:
                resolved.append(rid)
            else:
                hits = await self.find_node_by_name(rid, project_id)
                resolved.append(hits[0]["id"] if hits else rid)
        return resolved

    async def find_sibling_callers(
        self,
        node_id: str,
        class_id: str,
        project_id: str | None = None,
    ) -> list[str]:
        """Find callers of node_id that live in the same class.

        Checks both the fully-qualified callee_id AND the bare method name,
        because _resolve_callee leaves ambiguous callees as bare names when
        multiple classes share the same method name (e.g. 'handle').
        """
        bare_name = node_id.split(".")[-1]
        escaped = class_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT DISTINCT caller_id FROM edges "
            f"WHERE (callee_id=? OR callee_id=?) "
            f"AND caller_id LIKE ? ESCAPE '\\'{pid_clause}",
            (node_id, bare_name, f"{escaped}.%", *pid_args),
        ) as cur:
            return [r["caller_id"] for r in await cur.fetchall()]

    async def find_self_delegating_callees(
        self, caller_id: str, method_name: str, project_id: str | None = None
    ) -> list[str]:
        """Return callee_ids where callee_id ends with '.{method_name}'.

        Catches calls like self._next.handle(), child.render(), element.draw()
        that the indexer cannot resolve to a known node because the object is a
        runtime value. Used by CoR and Composite detectors.
        """
        escaped = method_name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT DISTINCT callee_id FROM edges "
            f"WHERE caller_id=? AND callee_id LIKE ? ESCAPE '\\'{pid_clause}",
            (caller_id, f"%.{escaped}", *pid_args),
        ) as cur:
            return [r["callee_id"] for r in await cur.fetchall()]

    async def get_node_body(self, node_id: str, project_id: str | None = None) -> str:
        """Fetch the stored body text for a single node. Returns '' if not found."""
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT body FROM nodes WHERE id=?{pid_clause} LIMIT 1",
            (node_id, *pid_args),
        ) as cur:
            row = await cur.fetchone()
        return (row["body"] or "") if row else ""

    async def get_node_abstractness(self, node_id: str, project_id: str | None = None) -> bool:
        """Return True if node_id is abstract (@abstractmethod or raise NotImplementedError body)."""
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT decorators, body FROM nodes WHERE id=?{pid_clause} LIMIT 1",
            (node_id, *pid_args),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return False
        decorators = json.loads(row["decorators"] or "[]")
        if "abstractmethod" in decorators:
            return True
        return "raise NotImplementedError" in (row["body"] or "")

    async def find_subclasses(
        self, class_id: str, project_id: str | None = None
    ) -> list[str]:
        """Return fully-qualified ids of classes that directly inherit from class_id."""
        pid_clause = " AND project_id=?" if project_id else ""
        pid_args = (project_id,) if project_id else ()
        async with self._db.execute(
            f"SELECT caller_id FROM edges "
            f"WHERE callee_id=? AND edge_type='inherits'{pid_clause}",
            (class_id, *pid_args),
        ) as cur:
            return [r["caller_id"] for r in await cur.fetchall()]

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
                SELECT n.id, n.name, n.file, n.module, n.signature, n.project_id, n.is_external, n.start_line, n.end_line, e.edge_type
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
                SELECT n.id, n.name, n.file, n.module, n.signature, n.project_id, n.is_external, n.start_line, n.end_line, e.edge_type
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
            f"SELECT {_NODE_COLS} FROM nodes WHERE id IN ({ph}){pid_where}",
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

    async def get_distinct_callee_names(self, project_id: str, limit: int = 80) -> list[str]:
        """Return a sample of distinct callee IDs from the project's call graph.

        Used by ContractManager.generate_draft to ground the LLM's prohibited_patterns
        in actual function names that appear in the codebase, rather than abstract
        semantic descriptions.
        """
        async with self._db.execute(
            "SELECT DISTINCT callee_id FROM edges WHERE project_id = ? LIMIT ?",
            (project_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [row[0] for row in rows]

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
        function_ids: list[str] | None = None,
    ) -> dict:
        """Persist a new contract record in draft status and return it."""
        import json
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            """INSERT INTO contracts
               (id, project_ids, function_ids, title, natural_language, rule_type,
                structural_expression, threshold, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)""",
            (contract_id, json.dumps(project_ids), json.dumps(function_ids or []),
             title, natural_language, rule_type, structural_expression, threshold, now),
        )
        await self._db.commit()
        return await self.get_contract(contract_id)

    async def get_contract(self, contract_id: str) -> dict | None:
        """Fetch a contract by ID, decoding JSON list fields."""
        import json
        async with self._db.execute(
            "SELECT * FROM contracts WHERE id = ?", (contract_id,)
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        r = dict(row)
        r["project_ids"] = json.loads(r["project_ids"])
        r["function_ids"] = json.loads(r.get("function_ids") or "[]")
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
            r["function_ids"] = json.loads(r.get("function_ids") or "[]")
            if project_id is None or project_id in r["project_ids"]:
                result.append(r)
        return result

    async def get_contracts_for_function(self, function_id: str, project_id: str) -> list[dict]:
        """Return active contracts that cover a specific function.

        Matching rules (checked in order per function_ids entry):
        - Empty function_ids → project-wide contract, always matches.
        - Entry ending in '.*' → prefix glob: matches any function whose ID
          starts with the prefix (e.g. 'myproject.EventBus.*' covers all
          current and future methods on EventBus).
        - Exact entry → matches only that function ID.
        """
        contracts = await self.list_contracts(project_id)
        active = [c for c in contracts if c["status"] == "active"]
        result = []
        for c in active:
            fids = c.get("function_ids") or []
            if not fids:
                result.append(c)
                continue
            for fid in fids:
                if fid.endswith(".*"):
                    if function_id.startswith(fid[:-1]):  # strip '*', keep trailing '.'
                        result.append(c)
                        break
                elif fid == function_id:
                    result.append(c)
                    break
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

    async def save_project_snapshot(
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

    # ── Count helpers ──────────────────────────────────────────────────────

    async def count_nodes(self) -> int:
        """Total node count across all projects."""
        async with self._db.execute("SELECT COUNT(*) FROM nodes") as cur:
            return (await cur.fetchone())[0]

    async def count_edges(self) -> int:
        """Total edge count across all projects."""
        async with self._db.execute("SELECT COUNT(*) FROM edges") as cur:
            return (await cur.fetchone())[0]

    async def count_decisions(self) -> int:
        """Total decision count across all projects."""
        async with self._db.execute("SELECT COUNT(*) FROM decisions") as cur:
            return (await cur.fetchone())[0]

    async def count_decision_function_links(self) -> int:
        """Count of distinct function IDs linked to any decision."""
        async with self._db.execute(
            "SELECT COUNT(DISTINCT function_id) FROM decision_functions"
        ) as cur:
            return (await cur.fetchone())[0]

    async def count_decision_embeddings(self) -> int:
        """Total decision embedding count."""
        async with self._db.execute("SELECT COUNT(*) FROM decision_embeddings") as cur:
            return (await cur.fetchone())[0]

    # ── Guidance layer accessors ───────────────────────────────────────────

    async def get_caller_counts(
        self, project_id: str, function_ids: list[str]
    ) -> dict[str, int]:
        """Count distinct callers in the project for each function ID in the list."""
        if not function_ids:
            return {}
        async with self._db.execute(
            """SELECT callee_id, COUNT(DISTINCT caller_id) AS cnt
               FROM edges
               WHERE project_id = ? AND callee_id = ANY(?)
               GROUP BY callee_id""",
            (project_id, function_ids),
        ) as cur:
            return {r["callee_id"]: r["cnt"] for r in await cur.fetchall()}

    async def get_functions_with_decisions(self, function_ids: list[str]) -> set[str]:
        """Return the subset of function_ids that have at least one logged decision."""
        if not function_ids:
            return set()
        async with self._db.execute(
            "SELECT DISTINCT function_id FROM decision_functions WHERE function_id = ANY(?)",
            (function_ids,),
        ) as cur:
            return {r["function_id"] for r in await cur.fetchall()}

    # ── Analysis data accessors ────────────────────────────────────────────
    # These methods exist so that analysis modules (performance, schema_objects)
    # do not access db._db directly.  Callers must not use db._db outside
    # src/call_graph/storage.py and src/embeddings/embedder.py.

    async def get_nodes_with_bodies(self, project_id: str) -> dict[str, dict]:
        """All nodes for a project keyed by id, including body text for detectors."""
        async with self._db.execute(
            "SELECT id, name, file, module, body FROM nodes WHERE project_id = ?",
            (project_id,),
        ) as cur:
            return {r["id"]: dict(r) for r in await cur.fetchall()}

    async def get_callee_map(self, project_id: str) -> dict[str, list[str]]:
        """All call edges for a project as caller_id → [callee_id, ...] dict."""
        async with self._db.execute(
            "SELECT caller_id, callee_id FROM edges WHERE project_id = ?",
            (project_id,),
        ) as cur:
            callee_map: dict[str, list[str]] = {}
            for e in await cur.fetchall():
                callee_map.setdefault(e["caller_id"], []).append(e["callee_id"])
            return callee_map

    async def get_acknowledged_performance_decisions(
        self, project_id: str
    ) -> dict[str, str]:
        """Return {function_id: description} for acknowledged Performance decisions."""
        return await self.get_acknowledged_decisions_by_type(project_id, "Performance")

    async def get_acknowledged_decisions_by_type(
        self, project_id: str, decision_type: str
    ) -> dict[str, str]:
        """Return {function_id: description} for acknowledged decisions of a given type."""
        async with self._db.execute(
            """
            SELECT df.function_id, d.description
            FROM decisions d
            JOIN decision_functions df ON df.decision_id = d.id
            WHERE d.project_id = ? AND d.type = ?
            """,
            (project_id, decision_type),
        ) as cur:
            return {r["function_id"]: r["description"] for r in await cur.fetchall()}

    async def get_db_schema(self, schema_name: str = "") -> dict[str, dict]:
        """Database schema for tables in the given schema: columns, FK refs, row estimates.

        Defaults to self._schema if set, otherwise 'public'.
        Returns a dict keyed by table_name, each value containing:
          columns: [{name, type}, ...]
          refs_out: [table, ...]   (tables this table references via FK)
          refs_in:  [table, ...]   (tables that reference this table via FK)
          row_count: int | None    (pg_class estimate, may be -1 before ANALYZE)
        """
        target_schema = schema_name or self._schema or "public"

        async with self._db.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = ?
            ORDER BY table_name, ordinal_position
            """,
            (target_schema,),
        ) as cur:
            col_rows = await cur.fetchall()

        tables: dict[str, dict] = {}
        for r in col_rows:
            tables.setdefault(r["table_name"], {
                "columns": [], "refs_out": [], "refs_in": [], "row_count": None
            })["columns"].append({"name": r["column_name"], "type": r["data_type"]})

        async with self._db.execute(
            """
            SELECT tc.table_name  AS from_table,
                   ccu.table_name AS to_table
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                ON tc.constraint_name = kcu.constraint_name
            JOIN information_schema.referential_constraints rc
                ON tc.constraint_name = rc.constraint_name
            JOIN information_schema.key_column_usage ccu
                ON rc.unique_constraint_name = ccu.constraint_name
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema = ?
            """,
            (target_schema,),
        ) as cur:
            for r in await cur.fetchall():
                if r["from_table"] in tables:
                    tables[r["from_table"]]["refs_out"].append(r["to_table"])
                if r["to_table"] in tables:
                    tables[r["to_table"]]["refs_in"].append(r["from_table"])

        async with self._db.execute(
            """
            SELECT relname AS table_name, reltuples::BIGINT AS row_estimate
            FROM pg_class
            WHERE relkind = 'r' AND relnamespace = (
                SELECT oid FROM pg_namespace WHERE nspname = ?
            )
            """,
            (target_schema,),
        ) as cur:
            for r in await cur.fetchall():
                if r["table_name"] in tables:
                    tables[r["table_name"]]["row_count"] = r["row_estimate"]

        return tables

    async def get_class_nodes(self, project_id: str) -> list[dict]:
        """All class-type nodes for a project (id, name, module, docstring, signature)."""
        async with self._db.execute(
            """
            SELECT id, name, module, docstring, signature
            FROM nodes WHERE project_id = ? AND type = 'class'
            """,
            (project_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_function_nodes_light(self, project_id: str) -> list[dict]:
        """All function-type nodes for a project (id, name, module only)."""
        async with self._db.execute(
            "SELECT id, name, module FROM nodes WHERE project_id = ? AND type = 'function'",
            (project_id,),
        ) as cur:
            return [dict(r) for r in await cur.fetchall()]

    async def get_decisions_by_ids(
        self, ids: list[str], project_id: str | None = None
    ) -> dict[str, dict]:
        """Fetch decisions by a list of IDs, optionally filtered to a project."""
        if not ids:
            return {}
        ph = ",".join("?" * len(ids))
        pid_clause = " AND project_id = ?" if project_id else ""
        async with self._db.execute(
            f"SELECT * FROM decisions WHERE id IN ({ph}){pid_clause}",
            [*ids, *((project_id,) if project_id else ())],
        ) as cur:
            return {r["id"]: dict(r) for r in await cur.fetchall()}

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
               WHERE k.key_hash = ? AND k.revoked_at IS NULL""",
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

    async def has_any_users(self) -> bool:
        """Return True if at least one user exists. Used to lock the /setup endpoint."""
        async with self._db.execute("SELECT 1 FROM users LIMIT 1") as cur:
            return await cur.fetchone() is not None

    async def list_api_keys(self, user_id: str) -> list[dict]:
        """Return active API keys for user_id, newest first."""
        async with self._db.execute(
            """SELECT id, name, created_at, last_used
               FROM api_keys
               WHERE user_id = ? AND revoked_at IS NULL
               ORDER BY created_at DESC""",
            (user_id,),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def revoke_api_key(self, key_id: str, user_id: str) -> bool:
        """Revoke a key by ID, scoped to user_id. Returns True if a row was updated."""
        now = datetime.now(timezone.utc).isoformat()
        result = await self._db.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND user_id = ? AND revoked_at IS NULL",
            (now, key_id, user_id),
        )
        await self._db.commit()
        return int(result.split()[-1]) > 0

    async def revoke_key_by_raw(self, raw_key: str, user_id: str) -> str | None:
        """Revoke the key matching raw_key for user_id. Returns the key ID, or None if not found."""
        import hashlib
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        async with self._db.execute(
            "SELECT id FROM api_keys WHERE key_hash = ? AND user_id = ? AND revoked_at IS NULL",
            (key_hash, user_id),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        key_id = row["id"] if hasattr(row, "__getitem__") else row[0]
        now = datetime.now(timezone.utc).isoformat()
        await self._db.execute(
            "UPDATE api_keys SET revoked_at = ? WHERE id = ?",
            (now, key_id),
        )
        await self._db.commit()
        return key_id


    async def get_accessible_project_ids(self, user_id: str) -> set[str]:
        """Return all project IDs this user can read: demo projects + explicitly granted projects."""
        ids: set[str] = set()
        async with self._db.execute("SELECT project_id FROM demo_projects") as cur:
            async for row in cur:
                ids.add(row[0])
        async with self._db.execute(
            "SELECT project_id FROM project_access WHERE user_id = $1", (user_id,)
        ) as cur:
            async for row in cur:
                ids.add(row[0])
        return ids

    async def grant_project_access(
        self, user_id: str, project_id: str, role: str = "owner"
    ) -> None:
        """Grant user_id the given role on project_id. No-op if already granted."""
        await self._db.execute(
            """INSERT INTO project_access (user_id, project_id, role)
               VALUES (?, ?, ?)
               ON CONFLICT (user_id, project_id) DO UPDATE SET role = excluded.role""",
            (user_id, project_id, role),
        )
        await self._db.commit()

    async def has_any_owner(self, project_id: str) -> bool:
        """Return True if any user has owner access to this project."""
        async with self._db.execute(
            "SELECT 1 FROM project_access WHERE project_id = ? AND role = 'owner' LIMIT 1",
            (project_id,),
        ) as cur:
            return await cur.fetchone() is not None

    async def check_project_access(
        self, user_id: str, project_id: str, operation: str
    ) -> bool:
        """Return True if user_id may perform operation on project_id.

        Demo projects: any authenticated user may read or write.
        Private projects: owner role allows read and write; viewer allows read only.
        """
        async with self._db.execute(
            "SELECT 1 FROM demo_projects WHERE project_id = ?", (project_id,)
        ) as cur:
            is_demo = await cur.fetchone() is not None

        if is_demo:
            return True

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
