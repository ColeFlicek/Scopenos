#!/usr/bin/env python3
"""
Migrate an existing Phronosis SQLite database to Postgres.

Usage:
    python scripts/migrate_sqlite_to_postgres.py \
        --sqlite /data/phronosis.db \
        --postgres postgresql://phronosis:phronosis@localhost/phronosis

Migrates all tables except embedding vectors (those must be re-indexed via
index_project / reembed_project since the binary format differs between
sqlite-vec float32 blobs and pgvector).

Run this once during the transition from SQLite to Postgres.
"""
import argparse
import asyncio
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

TABLES = [
    "projects",
    "nodes",
    "edges",
    "decisions",
    "decision_functions",
    "contracts",
    "contract_examples",
    "contract_violations",
    "agent_improvements",
    "project_home_snapshots",
    "dependency_fingerprints",
    "users",
    "api_keys",
    "project_access",
    "demo_projects",
]

# Columns that exist in old SQLite schema but not in Postgres schema (dropped during migration)
_DROP_COLS = {
    "nodes": {"is_external"},  # kept in Postgres schema — no action needed
}


async def migrate(sqlite_path: str, pg_dsn: str, dry_run: bool = False) -> None:
    import asyncpg
    from pgvector.asyncpg import register_vector

    print(f"Source:      {sqlite_path}")
    print(f"Destination: {pg_dsn}")
    if dry_run:
        print("DRY RUN — no data will be written")
    print()

    src = sqlite3.connect(sqlite_path)
    src.row_factory = sqlite3.Row

    async def _init(conn):
        await register_vector(conn)

    pool = await asyncpg.create_pool(pg_dsn, min_size=1, max_size=4, init=_init)

    # Apply schema first (idempotent — CREATE TABLE IF NOT EXISTS)
    schema = (Path(__file__).parent.parent / "schema.sql").read_text()
    async with pool.acquire() as conn:
        await conn.execute(schema)

    total_rows = 0
    for table in TABLES:
        cur = src.execute(f"SELECT * FROM {table}")
        cols_info = [d[0] for d in cur.description]
        rows = cur.fetchall()
        if not rows:
            print(f"  {table}: 0 rows — skip")
            continue

        # Filter to columns that exist in Postgres schema
        async with pool.acquire() as conn:
            pg_cols_result = await conn.fetch(
                """SELECT column_name FROM information_schema.columns
                   WHERE table_name = $1 ORDER BY ordinal_position""",
                table,
            )
        pg_cols = {r["column_name"] for r in pg_cols_result}
        valid_cols = [c for c in cols_info if c in pg_cols]
        skipped_cols = [c for c in cols_info if c not in pg_cols]

        if skipped_cols:
            print(f"  {table}: skipping columns not in Postgres schema: {skipped_cols}")

        ph = ", ".join(f"${i+1}" for i in range(len(valid_cols)))
        col_list = ", ".join(valid_cols)
        insert_sql = (
            f"INSERT INTO {table}({col_list}) VALUES({ph}) ON CONFLICT DO NOTHING"
        )

        data = []
        for row in rows:
            data.append(tuple(row[c] for c in valid_cols))

        print(f"  {table}: {len(data)} rows", end="")
        if not dry_run:
            async with pool.acquire() as conn:
                await conn.executemany(insert_sql, data)
            print(" ✓")
        else:
            print(" (dry run)")
        total_rows += len(data)

    print()
    print(f"Migration complete: {total_rows} rows copied across {len(TABLES)} tables.")
    print()
    print("NOTE: Embedding vectors are NOT migrated (format differs).")
    print("Run reembed_project(<project_id>) for each project to rebuild embeddings.")

    src.close()
    await pool.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Migrate Phronosis SQLite DB to Postgres.")
    parser.add_argument("--sqlite", required=True, help="Path to existing phronosis.db SQLite file")
    parser.add_argument(
        "--postgres",
        default="postgresql://phronosis:phronosis@localhost/phronosis",
        help="Postgres DSN (default: postgresql://phronosis:phronosis@localhost/phronosis)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print counts without writing")
    args = parser.parse_args()
    asyncio.run(migrate(args.sqlite, args.postgres, dry_run=args.dry_run))


if __name__ == "__main__":
    cli()
