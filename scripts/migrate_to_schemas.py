#!/usr/bin/env python3
"""
Migration: move per-project data from public schema into project schemas.

Run with:
  python scripts/migrate_to_schemas.py --dry-run          # preview with counts (default)
  python scripts/migrate_to_schemas.py --execute          # actually migrate
  python scripts/migrate_to_schemas.py --execute --db-url postgresql://...

  # Re-run only the decision tables (if all other tables were already migrated):
  python scripts/migrate_to_schemas.py --execute --decisions-only
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.call_graph.storage import derive_schema_name

# Canonical ordered list of all per-project tables.
# Order matters: FK parents before children.
# Derived from create_project_schema() in schema_org.sql.
PER_PROJECT_TABLES = [
    "nodes",
    "edges",
    "function_embeddings",
    "decisions",
    "decision_embeddings",   # no project_id — joined through decisions
    "decision_functions",    # no project_id — joined through decisions
    "agent_improvements",
    "project_home_snapshots",
    "dependency_fingerprints",
    "branch_function_changes",
    "commit_function_changes",
    "module_patterns",
    "schema_object_embeddings",
]

# Tables that must be migrated via JOIN to decisions (no project_id column).
# Scoped to a project by joining: decisions.project_id = $1.
JOIN_VIA_DECISIONS = {"decision_embeddings", "decision_functions"}

DECISION_ONLY_TABLES = ["decisions", "decision_embeddings", "decision_functions"]


async def _count_join_table(conn, table: str, pid: str) -> int:
    """Count rows in a join-via-decisions table that belong to project pid."""
    if table == "decision_embeddings":
        return await conn.fetchval(
            "SELECT COUNT(*) FROM public.decision_embeddings de "
            "JOIN public.decisions d ON d.id = de.id WHERE d.project_id = $1",
            pid,
        )
    if table == "decision_functions":
        return await conn.fetchval(
            "SELECT COUNT(*) FROM public.decision_functions df "
            "JOIN public.decisions d ON d.id = df.decision_id WHERE d.project_id = $1",
            pid,
        )
    raise ValueError(f"Unknown join table: {table}")


async def _migrate_join_table(conn, schema: str, table: str, pid: str) -> int:
    """Insert rows for a join-via-decisions table into the project schema."""
    if table == "decision_embeddings":
        count = await _count_join_table(conn, table, pid)
        if count:
            await conn.execute(
                f'INSERT INTO "{schema}".decision_embeddings (id, embedding) '
                "SELECT de.id, de.embedding "
                "FROM public.decision_embeddings de "
                "JOIN public.decisions d ON d.id = de.id "
                "WHERE d.project_id = $1 "
                "ON CONFLICT DO NOTHING",
                pid,
            )
        return count

    if table == "decision_functions":
        count = await _count_join_table(conn, table, pid)
        if count:
            await conn.execute(
                f'INSERT INTO "{schema}".decision_functions (decision_id, function_id) '
                "SELECT df.decision_id, df.function_id "
                "FROM public.decision_functions df "
                "JOIN public.decisions d ON d.id = df.decision_id "
                "WHERE d.project_id = $1 "
                "ON CONFLICT DO NOTHING",
                pid,
            )
        return count

    raise ValueError(f"Unknown join table: {table}")


async def migrate(
    db_url: str,
    dry_run: bool = True,
    decisions_only: bool = False,
) -> None:
    """Migrate per-project data from public schema into project-specific schemas.

    Args:
        db_url:          asyncpg DSN for the target org database.
        dry_run:         When True (default), print counts but make no writes.
        decisions_only:  When True, only migrate the three decision tables.
                         Use when all other tables have already been migrated.
    """
    import asyncpg
    from pgvector.asyncpg import register_vector

    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        # ── Step 1: Ensure schema_name column exists ───────────────────────────
        # No UNIQUE constraint yet — backfill first, add constraint last.
        await conn.execute("""
            ALTER TABLE projects ADD COLUMN IF NOT EXISTS schema_name TEXT DEFAULT ''
        """)

        # ── Step 2: Fetch all projects ─────────────────────────────────────────
        projects = await conn.fetch("SELECT id, schema_name FROM projects")
        print(f"Found {len(projects)} project(s)")
        if decisions_only:
            print("Mode: decisions-only (nodes/edges/embeddings skipped)")

        tables_to_migrate = DECISION_ONLY_TABLES if decisions_only else PER_PROJECT_TABLES

        migrated_count = 0
        for proj in projects:
            pid = proj["id"]
            stored_schema = proj["schema_name"] or ""
            schema = stored_schema if stored_schema else derive_schema_name(pid)
            print(f"\n  [{pid}] → schema [{schema}]")

            total_rows = 0

            for table in tables_to_migrate:
                if table in JOIN_VIA_DECISIONS:
                    count = await _count_join_table(conn, table, pid)
                else:
                    has_col = await conn.fetchval("""
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name   = $1
                          AND column_name  = 'project_id'
                    """, table)
                    if not has_col:
                        print(f"    {table}: skipped (no project_id column in public)")
                        continue
                    count = await conn.fetchval(
                        f'SELECT COUNT(*) FROM public."{table}" WHERE project_id = $1',
                        pid,
                    )

                label = "(would migrate)" if dry_run else "(migrated)"
                print(f"    {table}: {count} row(s) {label if count else '(nothing to migrate)'}")
                total_rows += count

            if dry_run:
                print(f"    → {total_rows} total row(s) would be moved")
                continue

            # ── Execute migration ──────────────────────────────────────────────
            await conn.execute("SELECT create_project_schema($1)", schema)

            for table in tables_to_migrate:
                if table in JOIN_VIA_DECISIONS:
                    await _migrate_join_table(conn, schema, table, pid)
                    continue

                has_col = await conn.fetchval("""
                    SELECT 1 FROM information_schema.columns
                    WHERE table_schema = 'public'
                      AND table_name   = $1
                      AND column_name  = 'project_id'
                """, table)
                if not has_col:
                    continue

                count = await conn.fetchval(
                    f'SELECT COUNT(*) FROM public."{table}" WHERE project_id = $1',
                    pid,
                )
                if not count:
                    continue

                # Exclude GENERATED columns — Postgres rejects explicit inserts for them
                col_rows = await conn.fetch(
                    """SELECT column_name
                       FROM information_schema.columns
                       WHERE table_schema = 'public' AND table_name = $1
                         AND is_generated = 'NEVER'
                       ORDER BY ordinal_position""",
                    table,
                )
                if col_rows:
                    cols = ", ".join(f'"{r["column_name"]}"' for r in col_rows)
                    await conn.execute(
                        f'INSERT INTO "{schema}"."{table}" ({cols}) '
                        f'SELECT {cols} FROM public."{table}" WHERE project_id = $1 '
                        f'ON CONFLICT DO NOTHING',
                        pid,
                    )
                else:
                    await conn.execute(
                        f'INSERT INTO "{schema}"."{table}" '
                        f'SELECT * FROM public."{table}" WHERE project_id = $1 '
                        f'ON CONFLICT DO NOTHING',
                        pid,
                    )

            await conn.execute(
                "UPDATE projects SET schema_name = $1 WHERE id = $2",
                schema, pid,
            )
            migrated_count += 1

        if dry_run:
            print(
                f"\nDry run complete — {len(projects)} project(s) previewed.\n"
                "Re-run with --execute to apply changes."
            )
            return

        # ── Step 3: Add UNIQUE constraint after backfill ───────────────────────
        try:
            await conn.execute("""
                ALTER TABLE projects
                ADD CONSTRAINT projects_schema_name_unique UNIQUE (schema_name)
            """)
            print("\nAdded UNIQUE constraint on projects.schema_name")
        except Exception as e:
            if "already exists" in str(e).lower():
                print("\nUNIQUE constraint already exists — skipping")
            else:
                raise

        print(f"\nMigration complete: {migrated_count} project(s) migrated.")

    finally:
        await conn.close()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate per-project data from public schema into project-specific schemas."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Preview row counts without writing anything (default).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually perform the migration.",
    )
    parser.add_argument(
        "--decisions-only",
        action="store_true",
        default=False,
        help="Only migrate decisions/decision_embeddings/decision_functions. "
             "Use when all other tables have already been migrated.",
    )
    parser.add_argument(
        "--db-url",
        default="",
        help="Postgres DSN for the org database. Defaults to ORG_DB_URL or DATABASE_URL env var.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    db_url = (
        args.db_url
        or os.getenv("ORG_DB_URL")
        or os.getenv("DATABASE_URL")
        or "postgresql://scopenos:scopenos@localhost/scopenos"
    )
    dry_run = not args.execute
    asyncio.run(migrate(db_url, dry_run=dry_run, decisions_only=args.decisions_only))
