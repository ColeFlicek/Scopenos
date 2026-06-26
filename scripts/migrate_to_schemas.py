#!/usr/bin/env python3
"""
Migration: move per-project data from public schema into project schemas.

Run with:
  python scripts/migrate_to_schemas.py --dry-run          # preview (default)
  python scripts/migrate_to_schemas.py --execute          # actually migrate
  python scripts/migrate_to_schemas.py --execute --db-url postgresql://...
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.call_graph.storage import derive_schema_name

# Per-project tables in the order they should be migrated.
# Derived from the create_project_schema() function body in schema_org.sql.
# Tables with FK dependencies on other per-project tables appear after their
# dependencies (e.g. decision_functions after decisions).
PER_PROJECT_TABLES = [
    "nodes",
    "edges",
    "function_embeddings",
    "decisions",
    "decision_embeddings",
    "decision_functions",
    "agent_improvements",
    "project_home_snapshots",
    "dependency_fingerprints",
    "branch_function_changes",
    "commit_function_changes",
    "module_patterns",
    "schema_object_embeddings",
]


async def migrate(db_url: str, dry_run: bool = True) -> None:
    """Migrate per-project data from public schema into project-specific schemas.

    Args:
        db_url:  asyncpg-compatible DSN for the target database.
        dry_run: When True (default), only prints what would happen; no writes.
    """
    import asyncpg
    from pgvector.asyncpg import register_vector

    conn = await asyncpg.connect(db_url)
    await register_vector(conn)
    try:
        # ── Step 1: Ensure schema_name column exists (NO UNIQUE yet) ──────────
        # We add the column without the UNIQUE constraint because existing rows
        # may have empty/duplicate schema_name values. We backfill first, then
        # add the constraint at the end after all rows have distinct values.
        await conn.execute("""
            ALTER TABLE projects ADD COLUMN IF NOT EXISTS schema_name TEXT DEFAULT ''
        """)

        # ── Step 2: Fetch all projects ─────────────────────────────────────────
        projects = await conn.fetch("SELECT id, schema_name FROM projects")
        print(f"Found {len(projects)} project(s) to migrate")

        migrated_count = 0
        for proj in projects:
            pid = proj["id"]
            stored_schema = proj["schema_name"] or ""
            schema = stored_schema if stored_schema else derive_schema_name(pid)
            print(f"  [{pid}] -> schema [{schema}]")

            if not dry_run:
                # Create the project schema (idempotent — uses IF NOT EXISTS)
                await conn.execute("SELECT create_project_schema($1)", schema)

                # Migrate each per-project table
                for table in PER_PROJECT_TABLES:
                    # Verify the table has a project_id column in public schema
                    has_col = await conn.fetchval("""
                        SELECT 1 FROM information_schema.columns
                        WHERE table_schema = 'public'
                          AND table_name   = $1
                          AND column_name  = 'project_id'
                    """, table)
                    if not has_col:
                        print(f"    {table}: skipped (no project_id column in public)")
                        continue

                    # Check how many rows belong to this project in public
                    count = await conn.fetchval(
                        f'SELECT COUNT(*) FROM public."{table}" WHERE project_id = $1',
                        pid,
                    )
                    if count:
                        await conn.execute(
                            f'INSERT INTO "{schema}"."{table}" '
                            f'SELECT * FROM public."{table}" WHERE project_id = $1 '
                            f'ON CONFLICT DO NOTHING',
                            pid,
                        )
                        print(f"    {table}: {count} row(s) migrated")
                    else:
                        print(f"    {table}: 0 rows (nothing to migrate)")

                # Update schema_name on the projects row
                await conn.execute(
                    "UPDATE projects SET schema_name = $1 WHERE id = $2",
                    schema, pid,
                )
                migrated_count += 1

        if not dry_run:
            # ── Step 3: Add UNIQUE constraint after backfill ───────────────────
            # All schema_name values are now distinct, so the constraint is safe.
            try:
                await conn.execute("""
                    ALTER TABLE projects
                    ADD CONSTRAINT projects_schema_name_unique UNIQUE (schema_name)
                """)
                print("Added UNIQUE constraint on projects.schema_name")
            except Exception as e:
                if "already exists" in str(e).lower():
                    print("UNIQUE constraint already exists — skipping")
                else:
                    raise

            print(f"\nMigration complete: {migrated_count} project(s) migrated.")
        else:
            print(
                f"\nDry run complete — {len(projects)} project(s) would be migrated.\n"
                "Re-run with --execute to apply changes."
            )
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
        help="Preview what would be migrated without writing anything (default).",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Actually perform the migration.",
    )
    parser.add_argument(
        "--db-url",
        default="",
        help="Postgres DSN. Defaults to DATABASE_URL env var.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    db_url = args.db_url or os.getenv(
        "DATABASE_URL",
        "postgresql://phronosis:phronosis@localhost/phronosis",
    )
    dry_run = not args.execute
    asyncio.run(migrate(db_url, dry_run=dry_run))
