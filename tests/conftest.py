"""
Shared fixtures for Scopenos tests.

GraphData and node construction helpers so individual test files
don't repeat boilerplate. Tests that need a GraphData call _graph();
tests that need a node dict call _node().

The `db` fixture provides a real CallGraphDB connected to the test database.
Each test gets a clean slate: all project schemas are dropped and control-plane
tables are truncated before the fixture yields.
"""
from __future__ import annotations

import os

import pytest
import pytest_asyncio

from src.call_graph.models import GraphData
from src.call_graph.storage import CallGraphDB

# ── Database fixture ──────────────────────────────────────────────────────────

_TEST_DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL", "")

# Tables in the public schema that need truncating between tests.
# Order matters: FK children before parents.
_TRUNCATE_TABLES = [
    "contract_violations",
    "contract_examples",
    "contracts",
    "project_access",
    "api_keys",
    "projects",
    "decisions",
    "decision_function_links",
    "performance_concerns",
    "solid_concerns",
    "agent_improvements",
    "users",
    "pattern_prototypes",
    "embedding_cache",
    "dependency_fingerprints",
    "project_snapshots",
    "demo_projects",
    "organizations",
]


async def _clean_db(pool) -> None:
    """Drop all non-system schemas and truncate control-plane tables."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT schema_name FROM information_schema.schemata "
            "WHERE schema_name NOT IN ('public','pg_catalog','information_schema','pg_toast') "
            "AND schema_name NOT LIKE 'pg_%'"
        )
        for row in rows:
            await conn.execute(f'DROP SCHEMA IF EXISTS "{row["schema_name"]}" CASCADE')

        # Only truncate tables that actually exist (schema may differ across versions)
        existing = await conn.fetch(
            "SELECT tablename FROM pg_tables "
            "WHERE schemaname = 'public' AND tablename = ANY($1)",
            _TRUNCATE_TABLES,
        )
        if existing:
            tables = ", ".join(row["tablename"] for row in existing)
            await conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")


@pytest_asyncio.fixture
async def db():
    """Real CallGraphDB against the test database, reset before each test.

    Skips automatically if no database URL is configured.
    Set TEST_DATABASE_URL (preferred) or DATABASE_URL to enable.
    """
    if not _TEST_DSN:
        pytest.skip("No database URL configured — set TEST_DATABASE_URL or DATABASE_URL")

    instance = await CallGraphDB.create(_TEST_DSN)
    await _clean_db(instance._pool)
    yield instance
    await instance.close()


def _node(
    node_id: str,
    *,
    type: str = "function",
    summary: str | None = None,
    docstring: str | None = None,
    body_hash: str = "abc123",
    decorators: str = "[]",
) -> dict:
    """Build a minimal node dict, inferring name and module from the dotted ID."""
    parts = node_id.split(".")
    return {
        "id": node_id,
        "name": parts[-1],
        "type": type,
        "module": ".".join(parts[:2]) if len(parts) >= 2 else parts[0],
        "summary": summary,
        "docstring": docstring,
        "body_hash": body_hash,
        "decorators": decorators,
    }


def _graph(
    *,
    project_id: str = "test",
    nodes: list[dict] | None = None,
    edges: list[tuple[str, str]] | None = None,
    caller_counts: dict[str, int] | None = None,
    churn: dict[str, int] | None = None,
    contracts: list[dict] | None = None,
    recent_violation_count: int = 0,
    recent_decisions: list[dict] | None = None,
    prev_snapshot: dict | None = None,
    current_hashes: dict[str, str] | None = None,
    decisions_since: list[dict] | None = None,
) -> GraphData:
    """Build a GraphData with sensible defaults for testing."""
    nodes = nodes or []
    return GraphData(
        project_id=project_id,
        nodes=nodes,
        edges=edges or [],
        caller_counts=caller_counts or {},
        churn=churn or {},
        contracts=contracts or [],
        recent_violation_count=recent_violation_count,
        recent_decisions=recent_decisions or [],
        prev_snapshot=prev_snapshot,
        current_hashes=current_hashes or {n["id"]: n["body_hash"] for n in nodes},
        decisions_since=decisions_since or [],
    )
