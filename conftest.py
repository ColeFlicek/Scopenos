import os
import sys
from pathlib import Path
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).parent))

# TEST_DATABASE_URL is the only env var that feeds test fixtures.
# DATABASE_URL is the production connection string and must never be used here.
# Production/test DB separation is enforced by the scopenos-test-guard pytest
# plugin (installed at /opt/test_guard_src, outside this repo) and by a
# PostgreSQL REVOKE CONNECT on the production database.
TEST_DSN = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://scopenos:scopenos@localhost/scopenos_test",
)

# Org-level public tables — these stay in public and are truncated between tests.
_ORG_TABLES = [
    "contract_violations", "contract_examples", "contracts",
    "project_access", "api_keys", "users",
    "projects",
    "embedding_cache", "pattern_prototypes",
    "demo_projects",
]

# Per-project tables that also exist in public schema as fallback copies.
# Tests using an org-level db fixture (no schema set) write here, so we
# must truncate them between tests just like the old conftest did.
_PUBLIC_PROJECT_TABLES = [
    "commit_function_changes", "branch_function_changes", "module_patterns",
    "schema_object_embeddings", "dependency_fingerprints",
    "project_home_snapshots", "agent_improvements",
    "decision_functions", "decision_embeddings", "decisions",
    "function_embeddings", "edges", "nodes",
]


@pytest_asyncio.fixture
async def db():
    """Postgres DB fixture — cleans state before and after each test."""
    from src.call_graph.storage import CallGraphDB
    instance = await CallGraphDB.create(TEST_DSN)

    async def _clean(conn):
        # Drop all non-system schemas (project schemas from previous tests)
        schemas = await conn.fetch(
            """SELECT schema_name FROM information_schema.schemata
               WHERE schema_name NOT IN ('public','pg_catalog','information_schema','pg_toast')
               AND schema_name NOT LIKE 'pg_%'"""
        )
        for row in schemas:
            sname = row["schema_name"]
            exists = await conn.fetchval(
                "SELECT 1 FROM projects WHERE schema_name = $1", sname
            )
            if exists:
                await conn.execute("SELECT drop_project_schema($1)", sname)
            else:
                await conn.execute(f'DROP SCHEMA IF EXISTS "{sname}" CASCADE')

        for table in _ORG_TABLES:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
        for table in _PUBLIC_PROJECT_TABLES:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")

    async with instance._pool.acquire() as conn:
        await _clean(conn)

    yield instance

    # Close project-scoped pools BEFORE cleanup (they reference schemas we're about to drop)
    for pdb in list(instance._project_dbs.values()):
        if pdb._db:
            await pdb._db.close()
    instance._project_dbs.clear()

    async with instance._pool.acquire() as conn:
        await _clean(conn)

    await instance.close()
