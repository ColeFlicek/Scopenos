from __future__ import annotations

import json

from .call_graph.models import ArchitectureSnapshot, GraphData

DEFAULT_HTTP_PATTERNS: tuple[str, ...] = (
    "router.get", "router.post", "router.put", "router.patch",
    "router.delete", "router.api_route", "router.head", "router.options",
    "app.get", "app.post", "app.put", "app.patch", "app.delete",
    "app.route", "app.api_route",
    "blueprint.route", "bp.route",
)


class ArchitectureAnalyzer:
    """
    Pure architectural analysis over a GraphData bundle.
    No I/O — all heuristics are sync and deterministic given the same input.
    """

    def __init__(self, http_patterns: tuple[str, ...] = DEFAULT_HTTP_PATTERNS) -> None:
        self._http_patterns = http_patterns

    def snapshot(self, data: GraphData) -> ArchitectureSnapshot:
        subsystems = self._build_subsystems(data)
        connections = self._cross_subsystem_connections(data)
        chokepoints = self._chokepoints(data)
        entry_points = self._entry_points(data)
        risk_surface, risk_mode = self._risk_surface(data)
        health = self._health(data, risk_mode)
        since = self._since_last_session(data)

        return ArchitectureSnapshot(
            project_id=data.project_id,
            function_count=len(data.nodes),
            subsystems=subsystems,
            connections=connections,
            chokepoints=chokepoints,
            entry_points=entry_points,
            risk_surface=risk_surface,
            health=health,
            recent_decisions=data.recent_decisions,
            since_last_session=since,
        )

    # ── Heuristics ────────────────────────────────────────────────────────

    def _subsystem(self, node_id: str) -> str:
        parts = node_id.split(".")
        return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]

    def _build_subsystems(self, data: GraphData) -> list[dict]:
        subsystem_nodes: dict[str, list[dict]] = {}
        for n in data.nodes:
            s = self._subsystem(n["id"])
            subsystem_nodes.setdefault(s, []).append(n)

        subsystems = []
        for s_name, nodes in sorted(subsystem_nodes.items(),
                                    key=lambda x: len(x[1]), reverse=True):
            anchor = next(
                (n for n in nodes if n["type"] in ("class", "ClassDef")), None
            )
            if anchor is None:
                anchor = max(nodes, key=lambda n: data.caller_counts.get(n["id"], 0))
            subsystems.append({
                "name": s_name,
                "function_count": len(nodes),
                "anchor": anchor["id"],
                "anchor_summary": (anchor.get("summary") or "")[:120],
            })
        return subsystems

    def _cross_subsystem_connections(self, data: GraphData) -> list[dict]:
        conn_counts: dict[tuple[str, str], int] = {}
        for caller_id, callee_id in data.edges:
            s_from = self._subsystem(caller_id)
            s_to = self._subsystem(callee_id)
            if s_from != s_to:
                key = (s_from, s_to)
                conn_counts[key] = conn_counts.get(key, 0) + 1

        return [
            {"from": k[0], "to": k[1], "edge_count": v}
            for k, v in sorted(conn_counts.items(), key=lambda x: -x[1])
            if v >= 2
        ]

    def _chokepoints(self, data: GraphData) -> list[dict]:
        all_node_ids = {n["id"] for n in data.nodes}
        return sorted(
            [
                {"id": nid, "caller_count": cnt}
                for nid, cnt in data.caller_counts.items()
                if nid in all_node_ids
            ],
            key=lambda x: -x["caller_count"],
        )[:5]

    def _entry_points(self, data: GraphData) -> list[dict]:
        callee_ids = {callee for _, callee in data.edges}
        entry_points = []
        for n in data.nodes:
            if n["type"] in ("class", "ClassDef"):
                continue
            decs = json.loads(n.get("decorators") or "[]")
            http_kind = any(
                any(p in d for p in self._http_patterns)
                for d in decs
            )
            if n["id"] not in callee_ids or http_kind:
                entry_points.append({
                    "id": n["id"],
                    "name": n["name"],
                    "kind": "http" if http_kind else "static",
                    "decorators": decs,
                })
        entry_points.sort(key=lambda x: (0 if x["kind"] == "http" else 1, x["id"]))
        return entry_points[:20]

    def _risk_surface(self, data: GraphData) -> tuple[list[dict], str]:
        risk = [
            {
                "id": fid,
                "churn": ch,
                "caller_count": data.caller_counts.get(fid, 0),
            }
            for fid, ch in data.churn.items()
            if ch >= 3 and data.caller_counts.get(fid, 0) >= 3
        ]
        risk.sort(key=lambda x: -(x["churn"] * x["caller_count"]))
        risk = risk[:5]

        if risk:
            return risk, "churn_and_callers"

        structural = sorted(
            [
                {"id": n["id"], "churn": 0, "caller_count": data.caller_counts.get(n["id"], 0)}
                for n in data.nodes
                if n["type"] not in ("class", "ClassDef")
                and data.caller_counts.get(n["id"], 0) >= 2
            ],
            key=lambda x: -x["caller_count"],
        )[:5]

        mode = "structural_heuristic_no_decisions" if structural else "insufficient_data"
        return structural, mode

    def _health(self, data: GraphData, risk_detection_mode: str) -> dict:
        documented = set(data.churn.keys())
        top_knowledge_gaps = sorted(
            [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "module": n["module"],
                    "caller_count": data.caller_counts.get(n["id"], 0),
                }
                for n in data.nodes
                if not n.get("summary") and not n.get("docstring")
                and n["id"] not in documented
                and n["type"] not in ("class", "ClassDef")
            ],
            key=lambda x: -x["caller_count"],
        )[:10]

        churn_hotspots = sorted(
            [{"id": fid, "decision_count": cnt} for fid, cnt in data.churn.items()],
            key=lambda x: -x["decision_count"],
        )[:5]

        return {
            "top_knowledge_gaps": top_knowledge_gaps,
            "churn_hotspots": churn_hotspots,
            "active_contract_count": len(data.contracts),
            "active_contracts": [{"id": c["id"], "title": c["title"]} for c in data.contracts],
            "recent_violation_count": data.recent_violation_count,
            "risk_detection_mode": risk_detection_mode,
        }

    def _since_last_session(self, data: GraphData) -> dict | None:
        if not data.prev_snapshot:
            return None

        prev_hashes: dict[str, str] = data.prev_snapshot.get("hashes", {})
        prev_time: str = data.prev_snapshot.get("captured_at", "")
        node_map = {n["id"]: n for n in data.nodes}

        new_ids = set(data.current_hashes) - set(prev_hashes)
        removed_ids = set(prev_hashes) - set(data.current_hashes)
        modified_ids = {
            nid for nid in data.current_hashes
            if nid in prev_hashes and data.current_hashes[nid] != prev_hashes[nid]
        }

        return {
            "since": prev_time,
            "functions_added": [
                {"id": nid, "name": node_map[nid]["name"], "module": node_map[nid]["module"]}
                for nid in sorted(new_ids) if nid in node_map
            ],
            "functions_modified": [
                {"id": nid, "name": node_map[nid]["name"], "module": node_map[nid]["module"]}
                for nid in sorted(modified_ids) if nid in node_map
            ],
            "functions_removed": sorted(removed_ids),
            "decisions_since": data.decisions_since,
        }
