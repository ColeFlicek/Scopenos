"""
Shared fixtures for Phronosis tests.

GraphData and node construction helpers so individual test files
don't repeat boilerplate. Tests that need a GraphData call _graph();
tests that need a node dict call _node().
"""
from src.call_graph.models import GraphData


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
