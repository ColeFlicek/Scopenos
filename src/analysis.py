from __future__ import annotations

import json
from collections import defaultdict

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

    # Overview limits — full detail available via get_subsystem_detail()
    _SUBSYSTEM_OVERVIEW_LIMIT = 30
    _CONNECTION_OVERVIEW_LIMIT = 30

    # Subsystem with more than this many functions gets a 3-part prefix
    # (e.g. "django.db" at 3817 fns splits into "django.db.models", etc.)
    _ADAPTIVE_DEPTH_THRESHOLD = 300

    # How many functions to surface per subsystem in the compact overview
    _TOP_FUNCTIONS_PER_SUBSYSTEM = 3

    def __init__(self, http_patterns: tuple[str, ...] = DEFAULT_HTTP_PATTERNS) -> None:
        self._http_patterns = http_patterns

    # ── Subsystem membership (computed once, shared by all methods) ───────────

    def _build_subsystem_map(self, data: GraphData) -> dict[str, str]:
        """Map node_id → subsystem_name using adaptive depth.

        Two-pass: first count 2-part prefix sizes, then promote oversized
        prefixes to 3-part paths so large packages don't collapse into one blob.
        """
        # Pass 1 — count 2-part prefix sizes
        counts_2: dict[str, int] = defaultdict(int)
        for n in data.nodes:
            counts_2[self._prefix(n["id"], 2)] += 1

        large_2 = frozenset(
            k for k, cnt in counts_2.items() if cnt > self._ADAPTIVE_DEPTH_THRESHOLD
        )

        # Pass 2 — assign membership, using 3-part for large prefixes
        result: dict[str, str] = {}
        for n in data.nodes:
            nid = n["id"]
            prefix_2 = self._prefix(nid, 2)
            if prefix_2 in large_2:
                result[nid] = self._prefix(nid, 3)
            else:
                result[nid] = prefix_2
        return result

    @staticmethod
    def _prefix(node_id: str, depth: int) -> str:
        parts = node_id.split(".")
        return ".".join(parts[:depth]) if len(parts) >= depth else node_id

    def _subsystem(self, node_id: str, subsystem_map: dict[str, str] | None = None) -> str:
        if subsystem_map is not None:
            return subsystem_map.get(node_id, self._prefix(node_id, 2))
        return self._prefix(node_id, 2)

    # ── Main entry point ──────────────────────────────────────────────────────

    def snapshot(self, data: GraphData) -> ArchitectureSnapshot:
        # Compute subsystem membership once; every sub-method uses the same map.
        subsystem_map = self._build_subsystem_map(data)

        # Pre-compute cross-subsystem callee sets for public API (improvement 3)
        external_callee_counts = self._external_callee_counts(data, subsystem_map)

        subsystems = self._build_subsystems(data, subsystem_map, external_callee_counts)
        connections = self._cross_subsystem_connections(data, subsystem_map)
        chokepoints = self._chokepoints(data)
        since = self._since_last_session(data)

        return ArchitectureSnapshot(
            project_id=data.project_id,
            function_count=len(data.nodes),
            subsystems=subsystems,
            connections=connections,
            chokepoints=chokepoints,
            recent_decisions=data.recent_decisions,
            since_last_session=since,
        )

    # ── Subsystems ────────────────────────────────────────────────────────────

    def _external_callee_counts(
        self, data: GraphData, subsystem_map: dict[str, str]
    ) -> dict[str, int]:
        """For each function, count how many distinct external subsystems call it.

        "External" means the caller is in a different subsystem from the callee.
        This identifies the public API boundary of each subsystem.
        """
        counts: dict[str, int] = defaultdict(int)
        for caller_id, callee_id in data.edges:
            s_from = self._subsystem(caller_id, subsystem_map)
            s_to = self._subsystem(callee_id, subsystem_map)
            if s_from != s_to:
                counts[callee_id] += 1
        return dict(counts)

    def _build_subsystems(
        self,
        data: GraphData,
        subsystem_map: dict[str, str] | None = None,
        external_callee_counts: dict[str, int] | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        if subsystem_map is None:
            subsystem_map = self._build_subsystem_map(data)
        if external_callee_counts is None:
            external_callee_counts = self._external_callee_counts(data, subsystem_map)
        subsystem_nodes: dict[str, list[dict]] = defaultdict(list)
        for n in data.nodes:
            subsystem_nodes[self._subsystem(n["id"], subsystem_map)].append(n)

        subsystems = []
        for s_name, nodes in sorted(subsystem_nodes.items(),
                                    key=lambda x: len(x[1]), reverse=True):
            # Anchor: most-called class (not just the first class encountered)
            class_nodes = [n for n in nodes if n["type"] in ("class", "ClassDef")]
            if class_nodes:
                anchor = max(class_nodes, key=lambda n: data.caller_counts.get(n["id"], 0))
            else:
                anchor = max(nodes, key=lambda n: data.caller_counts.get(n["id"], 0))

            fn_nodes = [n for n in nodes if n["type"] not in ("class", "ClassDef")]

            top_fns = sorted(fn_nodes, key=lambda n: data.caller_counts.get(n["id"], 0), reverse=True)
            top_fns = top_fns[:self._TOP_FUNCTIONS_PER_SUBSYSTEM]

            subsystems.append({
                "name": s_name,
                "function_count": len(nodes),
                "anchor": anchor["id"],
                "anchor_summary": (anchor.get("summary") or "")[:80],
                "top_functions": [
                    {
                        "name": n["name"],
                        "id": n["id"],
                        "callers": data.caller_counts.get(n["id"], 0),
                    }
                    for n in top_fns
                ],
            })

        cap = limit if limit is not None else self._SUBSYSTEM_OVERVIEW_LIMIT
        if cap and len(subsystems) > cap:
            overflow = len(subsystems) - cap
            subsystems = subsystems[:cap]
            subsystems.append({
                "name": f"... and {overflow} more subsystems",
                "function_count": 0,
                "anchor": "",
                "anchor_summary": "Call get_subsystem_detail(project_id, name) for any subsystem above.",
                "top_functions": [],
            })
        return subsystems

    # ── Connections ───────────────────────────────────────────────────────────

    def _cross_subsystem_connections(
        self,
        data: GraphData,
        subsystem_map: dict[str, str] | None = None,
        limit: int | None = None,
        subsystem_filter: str | None = None,
    ) -> list[dict]:
        if subsystem_map is None:
            subsystem_map = self._build_subsystem_map(data)
        internal = set(subsystem_map.values())

        conn_counts: dict[tuple[str, str], int] = defaultdict(int)

        for caller_id, callee_id in data.edges:
            s_from = self._subsystem(caller_id, subsystem_map)
            s_to = self._subsystem(callee_id, subsystem_map)
            if s_from == s_to:
                continue
            if s_from not in internal or s_to not in internal:
                continue
            if subsystem_filter and subsystem_filter not in (s_from, s_to):
                continue
            conn_counts[(s_from, s_to)] += 1

        rows = []
        for key, count in sorted(conn_counts.items(), key=lambda x: -x[1]):
            if count < 2:
                continue
            rows.append({"from": key[0], "to": key[1], "edge_count": count})

        cap = limit if limit is not None else self._CONNECTION_OVERVIEW_LIMIT
        if cap and len(rows) > cap:
            total = len(rows)
            rows = rows[:cap]
            rows.append({
                "from": "...",
                "to": f"({total - cap} more — use get_subsystem_detail for full list)",
                "edge_count": 0,
            })
        return rows

    # ── Subsystem detail (drill-down) ─────────────────────────────────────────

    def subsystem_detail(self, data: GraphData, subsystem_name: str) -> dict:
        """Full detail for one subsystem.

        Improvement 5: if exact name not found, auto-resolves to the closest
        ancestor subsystem (e.g. 'django.db.models.sql' → 'django.db') so
        agents don't need to know the exact stored name.
        """
        subsystem_map = self._build_subsystem_map(data)
        all_subsystems = set(subsystem_map.values())

        # Exact match first
        resolved_name = subsystem_name
        if subsystem_name not in all_subsystems:
            # Walk up the prefix tree to find the closest ancestor
            parts = subsystem_name.split(".")
            ancestor = None
            for depth in range(len(parts) - 1, 0, -1):
                candidate = ".".join(parts[:depth])
                if candidate in all_subsystems:
                    ancestor = candidate
                    break
            if ancestor:
                resolved_name = ancestor
            else:
                # Nothing found — suggest closest matches
                suggestions = sorted(
                    (s for s in all_subsystems if s.startswith(parts[0])),
                    key=len
                )[:5]
                return {
                    "error": f"No subsystem named {subsystem_name!r} found.",
                    "suggestions": suggestions,
                }

        note = (f"Note: '{subsystem_name}' maps to stored subsystem '{resolved_name}'."
                if resolved_name != subsystem_name else None)

        nodes_in = [n for n in data.nodes
                    if self._subsystem(n["id"], subsystem_map) == resolved_name]

        external_callee_counts = self._external_callee_counts(data, subsystem_map)

        # Most-called class as anchor
        class_nodes = [n for n in nodes_in if n["type"] in ("class", "ClassDef")]
        if class_nodes:
            anchor = max(class_nodes, key=lambda n: data.caller_counts.get(n["id"], 0))
        else:
            anchor = max(nodes_in, key=lambda n: data.caller_counts.get(n["id"], 0))

        fn_nodes = [n for n in nodes_in if n["type"] not in ("class", "ClassDef")]
        top_fns = sorted(fn_nodes, key=lambda n: data.caller_counts.get(n["id"], 0), reverse=True)[:50]

        # Public API: functions called from other subsystems, sorted by external caller count
        public_api = sorted(
            [n for n in fn_nodes if external_callee_counts.get(n["id"], 0) > 0],
            key=lambda n: external_callee_counts.get(n["id"], 0),
            reverse=True,
        )[:20]

        anchor_summary = anchor.get("summary") or ""
        top_names = [n["name"] for n in top_fns[:8]]
        role = (
            f"{anchor_summary[:120]}  Key functions: {', '.join(top_names)}."
        ).strip()

        subsystem_churn = sum(data.churn.get(n["id"], 0) for n in nodes_in)

        result = {
            "subsystem": resolved_name,
            "function_count": len(nodes_in),
            "anchor": anchor["id"],
            "anchor_summary": anchor_summary,
            "role": role,
            "churn": subsystem_churn,
            # Improvement 3: the public API — what other subsystems depend on
            "public_api": [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "external_callers": external_callee_counts.get(n["id"], 0),
                    "summary": (n.get("summary") or "")[:200],
                }
                for n in public_api
            ],
            "top_functions": [
                {
                    "id": n["id"],
                    "name": n["name"],
                    "caller_count": data.caller_counts.get(n["id"], 0),
                    "external_callers": external_callee_counts.get(n["id"], 0),
                    "summary": (n.get("summary") or "")[:200],
                }
                for n in top_fns
            ],
            "connections": self._cross_subsystem_connections(
                data, subsystem_map, limit=None, subsystem_filter=resolved_name
            ),
        }
        if note:
            result["note"] = note
        return result

    # ── Unchanged heuristics ──────────────────────────────────────────────────

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
