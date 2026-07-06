"""
Tests for src/guidance.py — the Guidance Layer signal classifier.

All pure-signal tests use no DB. DB-backed signal tests use a _DB stub
matching the two methods guidance.py needs: get_caller_counts and
get_functions_with_decisions, plus list_contracts.
"""
import asyncio
import pytest
from src.guidance import (
    Guidance,
    FollowUp,
    compute_guidance,
    _concentration_signal,
    _async_signal,
    _naming_signal,
    _chokepoint_follow_ups,
    _decision_gap_follow_ups,
    _contract_constraints,
    _performance_suggestion,
    CHOKEPOINT_THRESHOLD,
    compute_callers_guidance,
    compute_callees_guidance,
    compute_decision_guidance,
    compute_performance_guidance,
    PATTERN_CAUSE,
)


def run(coro):
    return asyncio.run(coro)


# ── Stubs ──────────────────────────────────────────────────────────────────────

class _DB:
    """Minimal stub matching the 3 CallGraphDB methods used by guidance.py."""

    def __init__(
        self,
        caller_counts: dict | None = None,
        functions_with_decisions: set | None = None,
        contracts: list | None = None,
    ):
        self._caller_counts = caller_counts or {}
        self._functions_with_decisions = functions_with_decisions or set()
        self._contracts = contracts or []

    async def get_caller_counts(self, project_id: str, function_ids: list) -> dict:
        return {k: v for k, v in self._caller_counts.items() if k in function_ids}

    async def get_functions_with_decisions(self, function_ids: list) -> set:
        return self._functions_with_decisions & set(function_ids)

    async def list_contracts(self, project_id: str | None = None) -> list:
        return self._contracts


def _result(
    fn_id: str,
    module: str = "src.mod",
    name: str = "",
    signature: str = "def fn():",
    similarity: float = 0.8,
    file: str = "src/mod.py",
) -> dict:
    return {
        "id": fn_id,
        "module": module,
        "name": name or fn_id.split(".")[-1],
        "signature": signature,
        "similarity": similarity,
        "file": file,
        "summary": "",
    }


def _contract(title: str, project_ids=None, function_ids=None, status="active") -> dict:
    return {
        "id": "c1",
        "title": title,
        "natural_language": title,
        "status": status,
        "project_ids": project_ids or ["proj"],
        "function_ids": function_ids or [],
    }


# ── Concentration signal ───────────────────────────────────────────────────────

class TestConcentrationSignal:
    def test_strong_concentration_above_75(self):
        results = [_result(f"src.auth.fn{i}", module="src.auth") for i in range(6)]
        results += [_result("src.util.fn", module="src.util")]
        results += [_result("src.util.fn2", module="src.util")]
        msg, conf = _concentration_signal(results)
        assert "6/8" in msg
        assert "src.auth" in msg
        assert conf >= 0.75

    def test_moderate_concentration_50_to_75(self):
        results = [_result(f"src.auth.fn{i}", module="src.auth") for i in range(5)]
        results += [_result(f"src.util.fn{i}", module="src.util") for i in range(5)]
        result = _concentration_signal(results)
        # 50/50 → no signal (below 75%)
        assert result is None

    def test_exactly_75_percent_fires(self):
        results = [_result(f"src.auth.fn{i}", module="src.auth") for i in range(3)]
        results += [_result("src.util.fn", module="src.util")]
        msg, conf = _concentration_signal(results)
        assert "3/4" in msg
        assert conf == pytest.approx(0.75)

    def test_single_result_fires(self):
        results = [_result("src.auth.fn", module="src.auth")]
        msg, conf = _concentration_signal(results)
        assert "src.auth" in msg
        assert conf == 1.0

    def test_empty_results_returns_none(self):
        assert _concentration_signal([]) is None

    def test_below_75_returns_none(self):
        results = [_result(f"src.m{i}.fn", module=f"src.m{i}") for i in range(8)]
        assert _concentration_signal(results) is None


# ── Async distribution signal ──────────────────────────────────────────────────

class TestAsyncSignal:
    def test_all_async_fires(self):
        results = [_result(f"fn{i}", signature="async def fn():") for i in range(5)]
        msg = _async_signal(results)
        assert msg is not None
        assert "async" in msg.lower()

    def test_all_sync_returns_none(self):
        results = [_result(f"fn{i}", signature="def fn():") for i in range(5)]
        assert _async_signal(results) is None

    def test_mixed_fires(self):
        async_results = [_result(f"fn{i}", signature="async def fn():") for i in range(3)]
        sync_results = [_result(f"fn{i+3}", signature="def fn():") for i in range(3)]
        msg = _async_signal(async_results + sync_results)
        assert msg is not None
        assert "mixed" in msg.lower()

    def test_mostly_async_fires(self):
        results = [_result(f"fn{i}", signature="async def fn():") for i in range(8)]
        results.append(_result("fn_sync", signature="def fn():"))
        msg = _async_signal(results)
        assert msg is not None
        assert "async" in msg.lower()

    def test_empty_returns_none(self):
        assert _async_signal([]) is None


# ── Naming convention signal ───────────────────────────────────────────────────

class TestNamingSignal:
    def test_dominant_get_prefix(self):
        results = [
            _result("fn", name="get_user"),
            _result("fn2", name="get_project"),
            _result("fn3", name="get_node"),
            _result("fn4", name="get_edge"),
            _result("fn5", name="create_edge"),
        ]
        msg = _naming_signal(results)
        assert msg is not None
        assert "get" in msg

    def test_class_method_strips_class_prefix(self):
        results = [
            _result("fn", name="DB.get_user"),
            _result("fn2", name="DB.get_project"),
            _result("fn3", name="DB.get_node"),
            _result("fn4", name="DB.get_edge"),
        ]
        msg = _naming_signal(results)
        assert msg is not None
        assert "get" in msg

    def test_no_dominant_returns_none(self):
        results = [
            _result("fn", name="get_user"),
            _result("fn2", name="create_project"),
            _result("fn3", name="delete_node"),
            _result("fn4", name="list_edges"),
            _result("fn5", name="check_health"),
        ]
        assert _naming_signal(results) is None

    def test_leading_underscore_stripped(self):
        results = [
            _result("fn", name="_gather_data"),
            _result("fn2", name="_gather_nodes"),
            _result("fn3", name="_gather_edges"),
            _result("fn4", name="_gather_files"),
        ]
        msg = _naming_signal(results)
        assert msg is not None
        assert "gather" in msg

    def test_empty_returns_none(self):
        assert _naming_signal([]) is None


# ── Chokepoint signal ──────────────────────────────────────────────────────────

class TestChokeointFollowUps:
    def test_high_caller_function_fires(self):
        results = [
            _result("src.db.execute", name="execute"),
            _result("src.db.fetch", name="fetch"),
        ]
        caller_counts = {"src.db.execute": CHOKEPOINT_THRESHOLD + 5}
        follow_ups = _chokepoint_follow_ups(caller_counts, results)
        assert len(follow_ups) >= 1
        assert any("execute" in f.args.get("function_name", "") for f in follow_ups)
        assert any(f.tool == "get_impact_radius" for f in follow_ups)

    def test_below_threshold_does_not_fire(self):
        results = [_result("src.db.execute", name="execute")]
        caller_counts = {"src.db.execute": CHOKEPOINT_THRESHOLD - 1}
        assert _chokepoint_follow_ups(caller_counts, results) == []

    def test_multiple_chokepoints_surfaced(self):
        results = [
            _result("fn.a", name="a"),
            _result("fn.b", name="b"),
        ]
        caller_counts = {
            "fn.a": CHOKEPOINT_THRESHOLD + 10,
            "fn.b": CHOKEPOINT_THRESHOLD + 2,
        }
        follow_ups = _chokepoint_follow_ups(caller_counts, results)
        assert len(follow_ups) == 2

    def test_ordered_by_caller_count_descending(self):
        results = [_result("fn.a", name="a"), _result("fn.b", name="b")]
        caller_counts = {"fn.a": 20, "fn.b": 50}
        follow_ups = _chokepoint_follow_ups(caller_counts, results)
        names = [f.args["function_name"] for f in follow_ups]
        assert names[0] == "b"  # higher count first

    def test_empty_caller_counts(self):
        results = [_result("fn.a", name="a")]
        assert _chokepoint_follow_ups({}, results) == []


# ── Decision gap signal ────────────────────────────────────────────────────────

class TestDecisionGapFollowUps:
    def test_high_caller_no_decision_fires(self):
        results = [_result("src.db.execute", name="execute")]
        caller_counts = {"src.db.execute": CHOKEPOINT_THRESHOLD + 5}
        with_decisions = set()
        follow_ups = _decision_gap_follow_ups(caller_counts, with_decisions, results)
        assert len(follow_ups) == 1
        assert follow_ups[0].tool == "get_decision_history"

    def test_high_caller_with_decision_does_not_fire(self):
        results = [_result("src.db.execute", name="execute")]
        caller_counts = {"src.db.execute": CHOKEPOINT_THRESHOLD + 5}
        with_decisions = {"src.db.execute"}
        assert _decision_gap_follow_ups(caller_counts, with_decisions, results) == []

    def test_low_caller_no_decision_does_not_fire(self):
        results = [_result("src.db.execute", name="execute")]
        caller_counts = {"src.db.execute": 3}
        with_decisions = set()
        assert _decision_gap_follow_ups(caller_counts, with_decisions, results) == []

    def test_mixed_fires_only_for_gap(self):
        results = [
            _result("fn.a", name="a"),
            _result("fn.b", name="b"),
        ]
        caller_counts = {"fn.a": CHOKEPOINT_THRESHOLD + 5, "fn.b": CHOKEPOINT_THRESHOLD + 5}
        with_decisions = {"fn.a"}  # fn.b has no decision
        follow_ups = _decision_gap_follow_ups(caller_counts, with_decisions, results)
        assert len(follow_ups) == 1
        assert "b" in follow_ups[0].args["function_name"]


# ── Contract signal ────────────────────────────────────────────────────────────

class TestContractConstraints:
    def test_active_contract_matching_project_fires(self):
        contracts = [_contract("All DB queries via execute", project_ids=["proj"])]
        constraints = _contract_constraints(contracts, "proj", [], [])
        assert len(constraints) == 1
        assert "All DB queries via execute" in constraints[0]

    def test_draft_contract_ignored(self):
        contracts = [_contract("Draft rule", project_ids=["proj"], status="draft")]
        constraints = _contract_constraints(contracts, "proj", [], [])
        assert constraints == []

    def test_function_id_match_fires(self):
        contracts = [_contract(
            "Route via execute",
            project_ids=["proj"],
            function_ids=["src.db.execute"],
            status="active",
        )]
        constraints = _contract_constraints(
            contracts, "proj", ["src.db.execute", "src.db.fetch"], []
        )
        assert len(constraints) == 1

    def test_wildcard_function_match(self):
        contracts = [_contract(
            "All DB methods",
            project_ids=["proj"],
            function_ids=["src.db.*"],
            status="active",
        )]
        constraints = _contract_constraints(
            contracts, "proj", ["src.db.execute", "src.db.fetch"], []
        )
        assert len(constraints) == 1

    def test_no_matching_contract(self):
        contracts = [_contract("Other project rule", project_ids=["other"])]
        constraints = _contract_constraints(contracts, "proj", [], [])
        assert constraints == []

    def test_empty_function_ids_matches_all(self):
        contracts = [_contract("Project-wide rule", project_ids=["proj"], function_ids=[])]
        constraints = _contract_constraints(contracts, "proj", ["any.fn"], [])
        assert len(constraints) == 1


# ── Performance suggestion ─────────────────────────────────────────────────────

class TestPerformanceSuggestion:
    def test_async_db_module_fires(self):
        results = [
            _result("src.call_graph.storage.fn", module="src.call_graph.storage",
                    signature="async def fn():"),
        ]
        follow_up = _performance_suggestion(results, "proj")
        assert follow_up is not None
        assert follow_up.tool == "check_performance"

    def test_sync_only_does_not_fire(self):
        results = [
            _result("src.call_graph.storage.fn", module="src.call_graph.storage",
                    signature="def fn():"),
        ]
        assert _performance_suggestion(results, "proj") is None

    def test_async_non_db_does_not_fire(self):
        results = [
            _result("src.utils.fn", module="src.utils", signature="async def fn():"),
        ]
        assert _performance_suggestion(results, "proj") is None

    def test_async_embeddings_module_fires(self):
        results = [
            _result("src.embeddings.store.fn", module="src.embeddings.embedder",
                    signature="async def fn():"),
        ]
        follow_up = _performance_suggestion(results, "proj")
        assert follow_up is not None


# ── Guidance dataclass ─────────────────────────────────────────────────────────

class TestGuidanceSerialization:
    def test_to_dict_shape(self):
        g = Guidance(
            pattern_signal="6/8 results in src.auth",
            confidence=0.75,
            active_constraints=["All requests via middleware"],
            signals=["Module is async-first"],
            suggested_follow_ups=[
                FollowUp(tool="get_impact_radius", args={"function_name": "fn"}, reason="chokepoint"),
            ],
        )
        d = g.to_dict()
        assert d["pattern_signal"] == "6/8 results in src.auth"
        assert d["confidence"] == 0.75
        assert d["active_constraints"] == ["All requests via middleware"]
        assert d["signals"] == ["Module is async-first"]
        assert len(d["suggested_follow_ups"]) == 1
        assert d["suggested_follow_ups"][0]["tool"] == "get_impact_radius"

    def test_to_dict_empty_is_valid(self):
        g = Guidance(
            pattern_signal="",
            confidence=0.0,
            active_constraints=[],
            signals=[],
            suggested_follow_ups=[],
        )
        d = g.to_dict()
        assert d["suggested_follow_ups"] == []


# ── compute_guidance integration ───────────────────────────────────────────────

class TestComputeGuidance:
    def test_returns_guidance_object(self):
        results = [_result(f"src.auth.fn{i}", module="src.auth") for i in range(4)]
        db = _DB()
        g = run(compute_guidance(results, db, "proj"))
        assert isinstance(g, Guidance)

    def test_concentration_surfaces_in_pattern_signal(self):
        results = [_result(f"src.auth.fn{i}", module="src.auth") for i in range(7)]
        results.append(_result("src.util.fn", module="src.util"))
        db = _DB()
        g = run(compute_guidance(results, db, "proj"))
        assert "src.auth" in g.pattern_signal

    def test_chokepoint_becomes_follow_up(self):
        results = [_result("src.db.execute", name="execute", module="src.db")]
        db = _DB(caller_counts={"src.db.execute": CHOKEPOINT_THRESHOLD + 20})
        g = run(compute_guidance(results, db, "proj"))
        tools = [f.tool for f in g.suggested_follow_ups]
        assert "get_impact_radius" in tools

    def test_decision_gap_becomes_follow_up(self):
        results = [_result("src.db.execute", name="execute", module="src.db")]
        db = _DB(
            caller_counts={"src.db.execute": CHOKEPOINT_THRESHOLD + 20},
            functions_with_decisions=set(),
        )
        g = run(compute_guidance(results, db, "proj"))
        tools = [f.tool for f in g.suggested_follow_ups]
        assert "get_decision_history" in tools

    def test_active_contract_becomes_constraint(self):
        results = [_result("src.db.execute", name="execute", module="src.db")]
        db = _DB(contracts=[
            _contract("All DB via execute", project_ids=["proj"])
        ])
        g = run(compute_guidance(results, db, "proj"))
        assert any("All DB via execute" in c for c in g.active_constraints)

    def test_empty_results_returns_empty_guidance(self):
        db = _DB()
        g = run(compute_guidance([], db, "proj"))
        assert g.pattern_signal == ""
        assert g.active_constraints == []
        assert g.suggested_follow_ups == []

    def test_no_db_calls_needed_for_pure_signals(self):
        """Pure signals (concentration, async, naming) work even with a no-op DB."""
        results = [
            _result(f"src.auth.fn{i}", module="src.auth",
                    name=f"get_user_{i}", signature="async def fn():")
            for i in range(6)
        ]
        results.append(_result("src.util.fn", module="src.util",
                               name="list_users", signature="async def fn():"))
        db = _DB()
        g = run(compute_guidance(results, db, "proj"))
        assert "src.auth" in g.pattern_signal
        assert any("async" in s.lower() for s in g.signals)
        assert any("get" in s.lower() for s in g.signals)


# ── compute_callers_guidance ───────────────────────────────────────────────────

def _caller(name: str, module: str = "src.mod", signature: str = "def fn():") -> dict:
    return {"id": f"{module}.{name}", "name": name, "module": module,
            "signature": signature, "file": f"{module.replace('.', '/')}.py",
            "is_external": 0}


class TestCallersGuidance:
    def test_no_callers_returns_entry_point_note(self):
        g = compute_callers_guidance([], "my_fn")
        assert "entry point" in g["note"].lower() or "no callers" in g["note"].lower()

    def test_many_callers_flags_chokepoint(self):
        callers = [_caller(f"fn{i}") for i in range(CHOKEPOINT_THRESHOLD + 2)]
        g = compute_callers_guidance(callers, "execute")
        assert any("chokepoint" in s.lower() for s in g["signals"])
        assert any(f["tool"] == "get_impact_radius" for f in g["suggested_follow_ups"])

    def test_concentrated_callers_noted(self):
        callers = [_caller(f"fn{i}", module="src.tests") for i in range(6)]
        callers.append(_caller("fn_other", module="src.app"))
        g = compute_callers_guidance(callers, "helper")
        assert any("src.tests" in s for s in g["signals"])

    def test_all_async_callers_noted(self):
        callers = [_caller(f"fn{i}", signature="async def fn():") for i in range(4)]
        g = compute_callers_guidance(callers, "helper")
        assert any("async" in s.lower() for s in g["signals"])

    def test_few_callers_no_chokepoint_signal(self):
        callers = [_caller("fn1"), _caller("fn2")]
        g = compute_callers_guidance(callers, "helper")
        assert not any("chokepoint" in s.lower() for s in g["signals"])


# ── completeness signal ────────────────────────────────────────────────────────

def _node(name: str, module: str) -> dict:
    return {"id": f"{module}.{name}", "name": name, "module": module,
            "file": f"{module.replace('.', '/')}.py"}


class TestCompletenessSignal:
    def test_other_implementations_fires_signal(self):
        from src.guidance import _completeness_signal
        others = [_node("get_group_by_cols", "django.db.models.expressions")]
        signal = _completeness_signal("get_group_by_cols", others)
        assert signal is not None
        assert "1 other" in signal.lower() or "other implementation" in signal.lower()

    def test_no_others_returns_none(self):
        from src.guidance import _completeness_signal
        signal = _completeness_signal("get_group_by_cols", [])
        assert signal is None

    def test_multiple_others_mentions_count(self):
        from src.guidance import _completeness_signal
        others = [
            _node("get_group_by_cols", "django.db.models.expressions"),
            _node("get_group_by_cols", "django.db.models.functions"),
        ]
        signal = _completeness_signal("get_group_by_cols", others)
        assert signal is not None
        assert "2" in signal

    def test_signal_suggests_query_similar(self):
        from src.guidance import _completeness_signal
        others = [_node("get_group_by_cols", "django.db.models.expressions")]
        signal = _completeness_signal("get_group_by_cols", others)
        assert "query_similar_functions" in signal

    def test_completeness_included_in_callers_guidance(self):
        callers = [_caller("fn1")]
        others = [_node("get_group_by_cols", "django.db.models.expressions")]
        g = compute_callers_guidance(callers, "get_group_by_cols", other_implementations=others)
        assert any("other" in s.lower() for s in g["signals"])


# ── GuidanceContext ────────────────────────────────────────────────────────────

class TestGuidanceContext:
    def test_new_context_has_not_seen_anything(self):
        from src.guidance import GuidanceContext
        ctx = GuidanceContext()
        assert not ctx.was_queried_similar("django.db.models.sql.query.Query.split_exclude")

    def test_record_marks_function_as_seen(self):
        from src.guidance import GuidanceContext
        ctx = GuidanceContext()
        ctx.record("query_similar_functions", ["django.db.models.sql.query.Query.split_exclude"])
        assert ctx.was_queried_similar("django.db.models.sql.query.Query.split_exclude")

    def test_was_queried_similar_only_true_for_query_similar_tool(self):
        from src.guidance import GuidanceContext
        ctx = GuidanceContext()
        ctx.record("get_callers", ["django.db.models.sql.query.Query.split_exclude"])
        assert not ctx.was_queried_similar("django.db.models.sql.query.Query.split_exclude")

    def test_should_resurface_when_never_queried(self):
        from src.guidance import GuidanceContext
        ctx = GuidanceContext()
        assert ctx.should_resurface_horizontal("django.db.models.sql.query.Query.split_exclude")

    def test_should_not_resurface_when_already_queried_similar(self):
        from src.guidance import GuidanceContext
        ctx = GuidanceContext()
        ctx.record("query_similar_functions", ["django.db.models.sql.query.Query.split_exclude"])
        assert not ctx.should_resurface_horizontal("django.db.models.sql.query.Query.split_exclude")

    def test_serialise_roundtrip(self):
        from src.guidance import GuidanceContext
        ctx = GuidanceContext()
        ctx.record("query_similar_functions", ["fn.a", "fn.b"])
        ctx2 = GuidanceContext.from_dict(ctx.to_dict())
        assert ctx2.was_queried_similar("fn.a")
        assert not ctx2.was_queried_similar("fn.c")


# ── compute_callees_guidance ───────────────────────────────────────────────────

def _callee(name: str, module: str = "src.mod", is_external: int = 0) -> dict:
    return {"id": f"{module}.{name}", "name": name, "module": module,
            "signature": "def fn():", "file": f"{module.replace('.', '/')}.py",
            "is_external": is_external}


class TestCalleesGuidance:
    def test_no_callees_returns_leaf_note(self):
        g = compute_callees_guidance([], "my_fn")
        assert "leaf" in g["note"].lower() or "nothing" in g["note"].lower()

    def test_external_callees_flagged(self):
        callees = [
            _callee("requests.get", module="requests", is_external=1),
            _callee("aiohttp.get", module="aiohttp", is_external=1),
            _callee("internal_fn", module="src.mod", is_external=0),
        ]
        g = compute_callees_guidance(callees, "my_fn")
        assert any("external" in s.lower() for s in g["signals"])

    def test_three_external_suggests_adapter(self):
        callees = [_callee(f"ext{i}", module=f"ext{i}", is_external=1) for i in range(3)]
        g = compute_callees_guidance(callees, "my_fn")
        assert any("adapter" in s.lower() for s in g["signals"])

    def test_internal_concentration_noted(self):
        callees = [_callee(f"fn{i}", module="src.storage") for i in range(5)]
        callees.append(_callee("other", module="src.utils"))
        g = compute_callees_guidance(callees, "my_fn")
        assert any("src.storage" in s for s in g["signals"])

    def test_note_includes_external_count(self):
        callees = [_callee("ext", module="ext", is_external=1), _callee("internal")]
        g = compute_callees_guidance(callees, "my_fn")
        assert "1 external" in g["note"]


# ── compute_decision_guidance ──────────────────────────────────────────────────

class TestDecisionGuidance:
    def test_empty_decisions_returns_informative_note(self):
        g = compute_decision_guidance([], "my_fn", "proj")
        assert "No decisions logged" in g["note"]
        tools = [f["tool"] for f in g["suggested_follow_ups"]]
        assert "log_decision" in tools
        assert "get_callers" in tools

    def test_non_empty_summarises_count_and_type(self):
        decisions = [
            {"type": "Design", "description": "Chose asyncpg over aiohttp for connection pooling"},
            {"type": "Patch", "description": "Fixed connection leak on timeout"},
        ]
        g = compute_decision_guidance(decisions, "my_fn", "proj")
        assert "2 decision" in g["note"]
        assert "Design" in g["note"] or "Patch" in g["note"]

    def test_non_empty_surfaces_most_recent(self):
        decisions = [
            {"type": "Design", "description": "Original design choice"},
            {"type": "Patch", "description": "Fixed the edge case in production"},
        ]
        g = compute_decision_guidance(decisions, "my_fn", "proj")
        assert "Fixed the edge case" in g["note"]

    def test_empty_has_no_follow_up_for_callers_without_project(self):
        g = compute_decision_guidance([], "fn", "")
        # Should still have follow_ups even with empty project_id
        assert len(g["suggested_follow_ups"]) >= 1


# ── compute_performance_guidance ──────────────────────────────────────────────

class _Finding:
    """Minimal stub matching Finding fields used by compute_performance_guidance."""
    def __init__(self, pattern: str, function_name: str, file: str = "src/mod.py",
                 suppressed: bool = False):
        self.pattern = pattern
        self.function_name = function_name
        self.file = file
        self.suppressed = suppressed


class TestPerformanceGuidance:
    def test_no_findings_returns_clean_note(self):
        g = compute_performance_guidance([])
        assert "No performance" in g["note"]
        assert g["structural_causes"] == []

    def test_all_suppressed_returns_acknowledged_note(self):
        findings = [_Finding("n_plus_one", "fn", suppressed=True)]
        g = compute_performance_guidance(findings)
        assert "acknowledged" in g["note"].lower()

    def test_active_finding_maps_to_structural_cause(self):
        findings = [_Finding("n_plus_one", "load_users")]
        g = compute_performance_guidance(findings)
        assert len(g["structural_causes"]) == 1
        assert "batch" in g["structural_causes"][0]["structural_cause"].lower()

    def test_sequential_awaits_maps_correctly(self):
        findings = [_Finding("sequential_awaits", "check_performance")]
        g = compute_performance_guidance(findings)
        cause = g["structural_causes"][0]["structural_cause"]
        assert "concurrency" in cause.lower()

    def test_all_patterns_covered_in_pattern_cause(self):
        for pattern in ["n_plus_one", "external_call_in_loop",
                        "correlated_join_aggregate", "sequential_awaits",
                        "quadratic_expansion"]:
            assert pattern in PATTERN_CAUSE

    def test_multiple_patterns_grouped(self):
        findings = [
            _Finding("n_plus_one", "fn1"),
            _Finding("n_plus_one", "fn2"),
            _Finding("sequential_awaits", "fn3"),
        ]
        g = compute_performance_guidance(findings)
        assert len(g["structural_causes"]) == 2
        counts = {c["pattern"]: c["count"] for c in g["structural_causes"]}
        assert counts["n_plus_one"] == 2
        assert counts["sequential_awaits"] == 1

    def test_follow_ups_include_impact_radius(self):
        findings = [_Finding("n_plus_one", "load_users")]
        g = compute_performance_guidance(findings)
        tools = [f["tool"] for f in g["suggested_follow_ups"]]
        assert "get_impact_radius" in tools
