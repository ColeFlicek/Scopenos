"""
Shared fixtures for Scopenos tests.

GraphData and node construction helpers so individual test files
don't repeat boilerplate. Tests that need a GraphData call _graph();
tests that need a node dict call _node().

The `db` fixture provides a real CallGraphDB connected to the test database.
The `project_id` fixture gives each test a stable, unique project namespace
so tests never interfere with one another — even when the database is shared
and persistent across runs.
"""
from __future__ import annotations

import os
import re

import pytest
import pytest_asyncio

from src.call_graph.models import GraphData
from src.call_graph.storage import CallGraphDB

# ── Database fixture ──────────────────────────────────────────────────────────

_TEST_DSN = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL", "")


@pytest_asyncio.fixture
async def db():
    """Real CallGraphDB against the test database.

    No cleanup — data persists across runs. Tests must use the project_id
    fixture to get their own namespace so they don't see each other's data.
    Skips automatically if no database URL is configured.
    """
    if not _TEST_DSN:
        pytest.skip("No database URL configured — set TEST_DATABASE_URL or DATABASE_URL")

    instance = await CallGraphDB.create(_TEST_DSN)
    yield instance
    await instance.close()


@pytest.fixture
def project_id(request) -> str:
    """Stable, unique project ID for the current test. Same value on every run.

    Each test gets its own namespace in the shared persistent test database.
    Running the same test twice upserts identical data (no-op) — no re-indexing
    is needed for expensive commits already stored from a previous run.

    Derived from test class + method name so the ID is human-readable and
    stable across sessions.
    """
    cls = request.node.cls.__name__ if request.node.cls else ""
    name = request.node.name
    raw = f"{cls}_{name}" if cls else name
    slug = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return f"t_{slug}"[:60]


# ── In-memory helpers ─────────────────────────────────────────────────────────

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
