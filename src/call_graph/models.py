from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GraphData:
    """Raw graph data fetched from storage — input to ArchitectureAnalyzer."""

    project_id: str
    nodes: list[dict]
    edges: list[tuple[str, str]]
    caller_counts: dict[str, int]
    churn: dict[str, int]
    contracts: list[dict]
    recent_violation_count: int
    recent_decisions: list[dict]
    prev_snapshot: dict | None          # {hashes: {id: hash}, captured_at: iso}
    current_hashes: dict[str, str]      # id → body_hash
    decisions_since: list[dict]         # decisions logged since prev_snapshot


@dataclass
class ArchitectureSnapshot:
    """Architectural intelligence snapshot — output of ArchitectureAnalyzer."""

    project_id: str
    function_count: int
    subsystems: list[dict]
    connections: list[dict]
    chokepoints: list[dict]
    entry_points: list[dict]
    risk_surface: list[dict]
    health: dict
    recent_decisions: list[dict]
    since_last_session: dict | None
