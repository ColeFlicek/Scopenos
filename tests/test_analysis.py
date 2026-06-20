"""
Tests for ArchitectureAnalyzer — the pure heuristic class extracted from CallGraphDB.

All tests pass plain GraphData objects built from dicts. No database, no async.
The fixtures live in conftest.py; this file only contains assertions.

Reading tip: each class tests one heuristic method. The integration test at the
bottom (TestSnapshot) checks that snapshot() wires all heuristics together correctly.
"""
import pytest
from src.analysis import ArchitectureAnalyzer, DEFAULT_HTTP_PATTERNS
from src.call_graph.models import GraphData
from tests.conftest import _node, _graph


ANALYZER = ArchitectureAnalyzer()


# ── _subsystem ────────────────────────────────────────────────────────────────

class TestSubsystem:
    def test_two_segment_id(self):
        assert ANALYZER._subsystem("src.server") == "src.server"

    def test_long_dotted_id_groups_at_two(self):
        assert ANALYZER._subsystem("src.call_graph.storage.CallGraphDB.commit") == "src.call_graph"

    def test_single_segment_returns_itself(self):
        assert ANALYZER._subsystem("scripts") == "scripts"

    def test_exactly_two_segments(self):
        assert ANALYZER._subsystem("src.analysis") == "src.analysis"


# ── _build_subsystems ─────────────────────────────────────────────────────────

class TestBuildSubsystems:
    def test_groups_nodes_by_first_two_segments(self):
        data = _graph(nodes=[
            _node("src.server.list_projects"),
            _node("src.server.get_home"),
            _node("src.call_graph.storage.commit"),
        ])
        subsystems = ANALYZER._build_subsystems(data)
        names = [s["name"] for s in subsystems]
        assert "src.server" in names
        assert "src.call_graph" in names

    def test_function_count_matches(self):
        data = _graph(nodes=[
            _node("src.server.a"),
            _node("src.server.b"),
            _node("src.server.c"),
        ])
        subsystems = ANALYZER._build_subsystems(data)
        assert subsystems[0]["function_count"] == 3

    def test_prefers_class_as_anchor(self):
        data = _graph(nodes=[
            _node("src.server.MyClass", type="class"),
            _node("src.server.some_function"),
        ])
        subsystems = ANALYZER._build_subsystems(data)
        assert subsystems[0]["anchor"] == "src.server.MyClass"

    def test_falls_back_to_most_called_when_no_class(self):
        data = _graph(
            nodes=[
                _node("src.server.popular"),
                _node("src.server.obscure"),
            ],
            caller_counts={"src.server.popular": 10, "src.server.obscure": 1},
        )
        subsystems = ANALYZER._build_subsystems(data)
        assert subsystems[0]["anchor"] == "src.server.popular"

    def test_sorted_by_size_descending(self):
        data = _graph(nodes=[
            _node("src.small.only_one"),
            _node("src.big.a"),
            _node("src.big.b"),
            _node("src.big.c"),
        ])
        subsystems = ANALYZER._build_subsystems(data)
        assert subsystems[0]["name"] == "src.big"


# ── _cross_subsystem_connections ──────────────────────────────────────────────

class TestCrossSubsystemConnections:
    def test_counts_cross_subsystem_edges(self):
        data = _graph(
            nodes=[_node("src.server.a"), _node("src.db.b")],
            edges=[
                ("src.server.a", "src.db.b"),
                ("src.server.a", "src.db.b"),  # two edges = meaningful
            ],
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert len(conns) == 1
        assert conns[0]["from"] == "src.server"
        assert conns[0]["to"] == "src.db"
        assert conns[0]["edge_count"] == 2

    def test_filters_same_subsystem_edges(self):
        data = _graph(
            nodes=[_node("src.server.a"), _node("src.server.b")],
            edges=[("src.server.a", "src.server.b"), ("src.server.a", "src.server.b")],
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert conns == []

    def test_filters_connections_with_fewer_than_two_edges(self):
        data = _graph(
            nodes=[_node("src.server.a"), _node("src.db.b")],
            edges=[("src.server.a", "src.db.b")],  # only one edge
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert conns == []

    def test_sorted_by_edge_count_descending(self):
        data = _graph(
            nodes=[
                _node("src.a.x"), _node("src.b.y"), _node("src.c.z"),
            ],
            edges=[
                ("src.a.x", "src.c.z"), ("src.a.x", "src.c.z"),  # a→c: 2
                ("src.a.x", "src.b.y"), ("src.a.x", "src.b.y"),
                ("src.a.x", "src.b.y"),  # a→b: 3
            ],
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert conns[0]["edge_count"] == 3
        assert conns[1]["edge_count"] == 2

    def test_filters_external_callee_subsystems(self):
        """SCIP adds external.* library nodes; they must not appear in wiring diagram."""
        data = _graph(
            nodes=[_node("src.server.a"), _node("src.db.b")],
            edges=[
                ("src.server.a", "external.asyncpg.pool.Pool.acquire"),
                ("src.server.a", "external.asyncpg.pool.Pool.acquire"),
                ("src.server.a", "external.asyncpg.pool.Pool.acquire"),
            ],
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert conns == []

    def test_filters_external_caller_subsystems(self):
        """Edges whose caller is an external stub are also excluded."""
        data = _graph(
            nodes=[_node("src.db.fn")],
            edges=[
                ("external.python-stdlib.os.path.join", "src.db.fn"),
                ("external.python-stdlib.os.path.join", "src.db.fn"),
            ],
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert conns == []

    def test_internal_connections_not_affected_by_external_filter(self):
        """Filtering external subsystems must not suppress internal wiring."""
        data = _graph(
            nodes=[_node("src.server.a"), _node("src.db.b")],
            edges=[
                ("src.server.a", "src.db.b"),
                ("src.server.a", "src.db.b"),
                # external edges alongside valid internal ones
                ("src.server.a", "external.asyncpg.pool.Pool.acquire"),
                ("src.server.a", "external.asyncpg.pool.Pool.acquire"),
            ],
        )
        conns = ANALYZER._cross_subsystem_connections(data)
        assert len(conns) == 1
        assert conns[0]["from"] == "src.server"
        assert conns[0]["to"] == "src.db"


# ── _chokepoints ──────────────────────────────────────────────────────────────

class TestChokepoints:
    def test_top_five_by_caller_count(self):
        nodes = [_node(f"src.server.fn_{i}") for i in range(7)]
        caller_counts = {f"src.server.fn_{i}": i for i in range(7)}
        data = _graph(nodes=nodes, caller_counts=caller_counts)
        choke = ANALYZER._chokepoints(data)
        assert len(choke) == 5
        assert choke[0]["caller_count"] == 6

    def test_excludes_external_library_calls(self):
        # "external.lib.func" appears in caller_counts but not in project nodes.
        # It should not be listed as a chokepoint.
        data = _graph(
            nodes=[_node("src.server.my_func")],
            caller_counts={
                "src.server.my_func": 5,
                "external.lib.func": 100,  # not a project node
            },
        )
        choke = ANALYZER._chokepoints(data)
        ids = [c["id"] for c in choke]
        assert "external.lib.func" not in ids
        assert "src.server.my_func" in ids

    def test_empty_graph_returns_empty(self):
        data = _graph()
        assert ANALYZER._chokepoints(data) == []


# ── _since_last_session ───────────────────────────────────────────────────────

class TestSinceLastSession:
    def test_returns_none_when_no_prev_snapshot(self):
        data = _graph(nodes=[_node("src.server.fn")])
        assert ANALYZER._since_last_session(data) is None

    def test_detects_added_functions(self):
        data = _graph(
            nodes=[_node("src.server.new_fn", body_hash="new")],
            prev_snapshot={"hashes": {}, "captured_at": "2026-01-01T00:00:00"},
            current_hashes={"src.server.new_fn": "new"},
        )
        since = ANALYZER._since_last_session(data)
        added_ids = [f["id"] for f in since["functions_added"]]
        assert "src.server.new_fn" in added_ids

    def test_detects_removed_functions(self):
        data = _graph(
            nodes=[],
            prev_snapshot={
                "hashes": {"src.server.old_fn": "abc"},
                "captured_at": "2026-01-01T00:00:00",
            },
            current_hashes={},
        )
        since = ANALYZER._since_last_session(data)
        assert "src.server.old_fn" in since["functions_removed"]

    def test_detects_modified_functions(self):
        data = _graph(
            nodes=[_node("src.server.fn", body_hash="new_hash")],
            prev_snapshot={
                "hashes": {"src.server.fn": "old_hash"},
                "captured_at": "2026-01-01T00:00:00",
            },
            current_hashes={"src.server.fn": "new_hash"},
        )
        since = ANALYZER._since_last_session(data)
        modified_ids = [f["id"] for f in since["functions_modified"]]
        assert "src.server.fn" in modified_ids

    def test_unchanged_function_not_in_any_list(self):
        data = _graph(
            nodes=[_node("src.server.stable", body_hash="same")],
            prev_snapshot={
                "hashes": {"src.server.stable": "same"},
                "captured_at": "2026-01-01T00:00:00",
            },
            current_hashes={"src.server.stable": "same"},
        )
        since = ANALYZER._since_last_session(data)
        assert since["functions_added"] == []
        assert since["functions_removed"] == []
        assert since["functions_modified"] == []

    def test_since_timestamp_matches_prev_snapshot(self):
        data = _graph(
            nodes=[],
            prev_snapshot={"hashes": {}, "captured_at": "2026-06-12T00:00:00"},
            current_hashes={},
        )
        since = ANALYZER._since_last_session(data)
        assert since["since"] == "2026-06-12T00:00:00"


# ── snapshot (integration) ────────────────────────────────────────────────────

class TestSnapshot:
    def test_snapshot_returns_correct_project_id(self):
        data = _graph(project_id="my_project")
        snap = ANALYZER.snapshot(data)
        assert snap.project_id == "my_project"

    def test_function_count_matches_node_count(self):
        data = _graph(nodes=[_node("src.a.x"), _node("src.a.y"), _node("src.b.z")])
        snap = ANALYZER.snapshot(data)
        assert snap.function_count == 3

    def test_recent_decisions_passed_through(self):
        decisions = [{"id": "abc", "type": "Patch", "description": "fix", "created_at": "now", "function_ids": []}]
        data = _graph(recent_decisions=decisions)
        snap = ANALYZER.snapshot(data)
        assert snap.recent_decisions == decisions

    def test_full_snapshot_is_serializable(self):
        import dataclasses, json
        data = _graph(nodes=[_node("src.server.fn")])
        snap = ANALYZER.snapshot(data)
        # dataclasses.asdict + json.dumps is how server.py serializes this.
        result = json.dumps(dataclasses.asdict(snap))
        assert "src.server" in result


# ── adaptive subsystem depth ──────────────────────────────────────────────────

class TestAdaptiveSubsystemDepth:
    def test_small_subsystem_uses_two_parts(self):
        # 5 functions in pkg.sub → threshold not exceeded → stays as "pkg.sub"
        data = _graph(nodes=[_node(f"pkg.sub.fn_{i}") for i in range(5)])
        smap = ANALYZER._build_subsystem_map(data)
        assert smap["pkg.sub.fn_0"] == "pkg.sub"

    def test_large_subsystem_uses_three_parts(self):
        # 350 functions in pkg.sub.a — exceeds threshold → uses "pkg.sub.a"
        nodes = [_node(f"pkg.sub.a.fn_{i}") for i in range(350)]
        data = _graph(nodes=nodes)
        smap = ANALYZER._build_subsystem_map(data)
        # The 2-part prefix "pkg.sub" has 350 > 300 → promotes to 3-part
        assert smap["pkg.sub.a.fn_0"] == "pkg.sub.a"

    def test_large_prefix_splits_into_children(self):
        # Two child packages under the same large 2-part prefix split independently
        nodes = ([_node(f"pkg.sub.models.fn_{i}") for i in range(200)] +
                 [_node(f"pkg.sub.backends.fn_{i}") for i in range(200)])
        data = _graph(nodes=nodes)
        smap = ANALYZER._build_subsystem_map(data)
        names = set(smap.values())
        assert "pkg.sub.models" in names
        assert "pkg.sub.backends" in names
        assert "pkg.sub" not in names  # bare 2-part prefix suppressed





# ── subsystem_detail prefix resolution ───────────────────────────────────────

class TestSubsystemDetailResolution:
    def test_exact_name_resolves_directly(self):
        data = _graph(nodes=[_node("src.server.fn")])
        result = ANALYZER.subsystem_detail(data, "src.server")
        assert result["subsystem"] == "src.server"
        assert "error" not in result

    def test_deep_name_resolves_to_ancestor(self):
        # "src.server.routes.admin" doesn't exist as a stored subsystem,
        # but "src.server" does → should resolve to it
        data = _graph(nodes=[_node("src.server.fn")])
        result = ANALYZER.subsystem_detail(data, "src.server.routes.admin")
        assert result["subsystem"] == "src.server"
        assert "note" in result  # explains the resolution

    def test_unknown_name_returns_error_with_suggestions(self):
        data = _graph(nodes=[_node("src.server.fn"), _node("src.db.fn")])
        result = ANALYZER.subsystem_detail(data, "completely.unknown")
        assert "error" in result
        assert "suggestions" in result
