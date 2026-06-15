import os
import sys
from pathlib import Path

import pytest_asyncio

# Make `src` importable as a top-level package from the project root.
sys.path.insert(0, str(Path(__file__).parent))

TEST_DSN = os.getenv("DATABASE_URL", "postgresql://phronosis:phronosis@localhost/phronosis_test")

_TRUNCATE_TABLES = [
    "decision_functions", "decisions",
    "contract_violations", "contract_examples", "contracts",
    "agent_improvements", "edges", "nodes",
    "dependency_fingerprints", "project_home_snapshots",
    "function_embeddings", "decision_embeddings",
    "api_keys", "project_access", "demo_projects", "users",
    "projects",
]


@pytest_asyncio.fixture
async def db():
    """Postgres DB fixture — truncates all tables before each test for isolation."""
    from src.call_graph.storage import CallGraphDB
    instance = await CallGraphDB.create(TEST_DSN)
    # Clean slate before the test
    async with instance._pool.acquire() as conn:
        for table in _TRUNCATE_TABLES:
            await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
    yield instance
    await instance.close()
