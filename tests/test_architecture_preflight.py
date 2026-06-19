"""
Tests for src/architecture_preflight.py.

All tests use in-memory stubs — no database, no embeddings service.
"""
import asyncio
import pytest
from src.architecture_preflight import (
    CouplingHotspot,
    ExternalScatter,
    DuplicationCluster,
    ArchitecturePreflight,
    _gather_coupling_hotspots,
    _gather_external_scatter,
    _gather_performance_signals,
    run_preflight,
)
from src.performance import Finding


def run(coro):
    return asyncio.run(coro)


# ── Stubs ─────────────────────────────────────────────────────────────────────

class _DB:
    """Minimal stub matching the CallGraphDB methods used by preflight."""

    def __init__(
        self,
        nodes: list[dict] | None = None,
        internal_ids: set[str] | None = None,
        callee_map: dict[str, list[str]] | None = None,
        ext_deps: list[dict] | None = None,
    ):
        self._nodes = nodes or []
        self._internal_ids = internal_ids or set()
        self._callee_map = callee_map or {}
        self._ext_deps = ext_deps or []

    async def get_all_nodes(self, project_id: str) -> list[dict]:
        return self._nodes

    async def get_internal_node_ids(self, project_id: str) -> set[str]:
        return self._internal_ids

    async def get_callee_map(self, project_id: str) -> dict[str, list[str]]:
        return self._callee_map

    async def list_external_dependencies(self, project_id: str) -> list[dict]:
        return self._ext_deps


def _node(nid: str, file: str = "src/mod.py", name: str = "") -> dict:
    return {"id": nid, "name": name or nid.split(".")[-1], "file": file, "module": nid}


def _finding(pattern: str, file: str, suppressed: bool = False) -> Finding:
    return Finding(
        function_id="x",
        function_name="fn",
        file=file,
        pattern=pattern,
        severity="high",
        detail="detail",
        suppressed=suppressed,
    )


# ── CouplingHotspot ───────────────────────────────────────────────────────────

class TestGatherCouplingHotspots:
    def test_high_fan_in_fan_out_is_flagged(self):
        # A → B, C → B, D → B  (fan-in for B = 3)
        # B → E, F, G          (fan-out for B = 3)
        internal = {"A", "B", "C", "D", "E", "F", "G"}
        nodes = [_node(nid) for nid in internal]
        callee_map = {
            "A": ["B"],
            "C": ["B"],
            "D": ["B"],
            "B": ["E", "F", "G"],
        }
        db = _DB(nodes=nodes, internal_ids=internal, callee_map=callee_map)
        hotspots = run(_gather_coupling_hotspots(db, "proj", min_score=4))
        assert any(h.function_id == "B" for h in hotspots)
        b = next(h for h in hotspots if h.function_id == "B")
        assert b.fan_in == 3
        assert b.fan_out == 3
        assert b.score == 9

    def test_sorted_by_score_descending(self):
        # Two hubs: B (3×3=9) and C (2×2=4)
        internal = {"A", "B", "C", "D", "E", "F", "G", "H"}
        callee_map = {
            "A": ["B"], "D": ["B"], "G": ["B"],  # B fan-in=3
            "B": ["E", "F", "H"],                 # B fan-out=3
            "A": ["C"], "D": ["C"],               # C fan-in=2
            "C": ["E", "F"],                      # C fan-out=2
        }
        # Note: dict has A mapped twice; Python keeps last. Let's use distinct keys.
        callee_map = {
            "A": ["B", "C"],
            "D": ["B", "C"],
            "G": ["B"],
            "B": ["E", "F", "H"],
            "C": ["E", "F"],
        }
        nodes = [_node(nid) for nid in internal]
        db = _DB(nodes=nodes, internal_ids=internal, callee_map=callee_map)
        hotspots = run(_gather_coupling_hotspots(db, "proj", min_score=4))
        scores = [h.score for h in hotspots]
        assert scores == sorted(scores, reverse=True)

    def test_low_score_excluded(self):
        # A → B only (fan-in=1, fan-out=0 → score=0)
        internal = {"A", "B"}
        db = _DB(
            nodes=[_node("A"), _node("B")],
            internal_ids=internal,
            callee_map={"A": ["B"]},
        )
        hotspots = run(_gather_coupling_hotspots(db, "proj", min_score=4))
        assert hotspots == []

    def test_external_callees_excluded_from_fan_out(self):
        # A calls B (internal) and EXT (external) — fan-out should only count B
        internal = {"A", "B"}
        callee_map = {"A": ["B", "EXT"], "X": ["A"], "Y": ["A"], "Z": ["A"]}
        internal = {"A", "B", "X", "Y", "Z"}
        db = _DB(
            nodes=[_node(nid) for nid in internal | {"EXT"}],
            internal_ids=internal,
            callee_map=callee_map,
        )
        hotspots = run(_gather_coupling_hotspots(db, "proj", min_score=1))
        a = next((h for h in hotspots if h.function_id == "A"), None)
        assert a is not None
        assert a.fan_out == 1  # only B, not EXT

    def test_empty_graph_returns_empty(self):
        db = _DB()
        assert run(_gather_coupling_hotspots(db, "proj")) == []


# ── ExternalScatter ───────────────────────────────────────────────────────────

class TestGatherExternalScatter:
    def _make_db(self, int_nodes, int_ids, callee_map, ext_deps):
        nodes = [_node(nid, file=f"src/{nid}.py") for nid in int_ids]
        return _DB(nodes=nodes, internal_ids=int_ids,
                   callee_map=callee_map, ext_deps=ext_deps)

    def test_library_used_from_many_files_is_scattered(self):
        ext_deps = [{"library": "httpx", "symbol_count": 1,
                     "symbols": [{"id": "httpx.get", "name": "get",
                                  "signature": "", "caller_count": 3}]}]
        callee_map = {"A": ["httpx.get"], "B": ["httpx.get"], "C": ["httpx.get"]}
        int_nodes = ["A", "B", "C"]
        db = self._make_db(int_nodes, set(int_nodes), callee_map, ext_deps)
        scatter = run(_gather_external_scatter(db, "proj"))
        assert len(scatter) == 1
        assert scatter[0].library == "httpx"
        assert scatter[0].caller_file_count == 3
        assert scatter[0].is_scattered

    def test_library_used_from_one_file_is_contained(self):
        ext_deps = [{"library": "stripe", "symbol_count": 1,
                     "symbols": [{"id": "stripe.charge", "name": "charge",
                                  "signature": "", "caller_count": 2}]}]
        # A and B are in the same file (both named "A" for simplicity → same file)
        nodes = [
            {"id": "A", "name": "A", "file": "src/payments.py", "module": "A"},
            {"id": "B", "name": "B", "file": "src/payments.py", "module": "B"},
        ]
        db = _DB(nodes=nodes, internal_ids={"A", "B"},
                 callee_map={"A": ["stripe.charge"], "B": ["stripe.charge"]},
                 ext_deps=ext_deps)
        scatter = run(_gather_external_scatter(db, "proj"))
        assert scatter[0].caller_file_count == 1
        assert not scatter[0].is_scattered

    def test_no_external_deps_returns_empty(self):
        db = _DB()
        assert run(_gather_external_scatter(db, "proj")) == []


# ── PerformanceStructureSignal ────────────────────────────────────────────────

class TestGatherPerformanceSignals:
    def test_n_plus_one_maps_to_repository_cause(self):
        findings = [_finding("n_plus_one", "src/server.py")]
        signals = _gather_performance_signals(findings)
        assert len(signals) == 1
        assert "batch" in signals[0].structural_cause.lower() or "repository" in signals[0].structural_cause.lower()

    def test_suppressed_findings_excluded(self):
        findings = [
            _finding("n_plus_one", "src/server.py", suppressed=False),
            _finding("n_plus_one", "src/other.py", suppressed=True),
        ]
        signals = _gather_performance_signals(findings)
        assert signals[0].count == 1
        assert "src/other.py" not in signals[0].affected_files

    def test_grouped_by_pattern(self):
        findings = [
            _finding("n_plus_one", "a.py"),
            _finding("n_plus_one", "b.py"),
            _finding("external_call_in_loop", "c.py"),
        ]
        signals = _gather_performance_signals(findings)
        assert len(signals) == 2
        n1 = next(s for s in signals if s.pattern == "n_plus_one")
        assert n1.count == 2
        assert set(n1.affected_files) == {"a.py", "b.py"}

    def test_unknown_pattern_gets_generic_cause(self):
        findings = [_finding("future_pattern", "x.py")]
        signals = _gather_performance_signals(findings)
        assert signals[0].structural_cause != ""

    def test_empty_findings_returns_empty(self):
        assert _gather_performance_signals([]) == []


# ── ArchitecturePreflight.to_brief ────────────────────────────────────────────

class TestToBrief:
    def _make(self, **kwargs) -> ArchitecturePreflight:
        return ArchitecturePreflight(
            project_id="test",
            coupling_hotspots=kwargs.get("coupling_hotspots", []),
            external_scatter=kwargs.get("external_scatter", []),
            duplication_clusters=kwargs.get("duplication_clusters", []),
            performance_signals=kwargs.get("performance_signals", []),
        )

    def test_brief_contains_project_id(self):
        brief = self._make().to_brief()
        assert "test" in brief

    def test_coupling_hotspot_appears_in_brief(self):
        h = CouplingHotspot("fn.id", "my_function", "src/a.py", fan_in=5, fan_out=4)
        brief = self._make(coupling_hotspots=[h]).to_brief()
        assert "my_function" in brief
        assert "20" in brief  # score = 5 * 4

    def test_scattered_library_flagged_in_brief(self):
        s = ExternalScatter("httpx", symbol_count=2, caller_file_count=4, caller_count=10)
        brief = self._make(external_scatter=[s]).to_brief()
        assert "httpx" in brief
        assert "Scattered" in brief or "adapter" in brief.lower()

    def test_contained_library_still_present_but_not_flagged(self):
        s = ExternalScatter("stripe", symbol_count=1, caller_file_count=1, caller_count=2)
        brief = self._make(external_scatter=[s]).to_brief()
        assert "stripe" in brief
        # Should NOT say "Scattered" for a contained library
        # (it appears under "Contained" heading)
        assert "Contained" in brief or "acceptable" in brief.lower()

    def test_duplication_cluster_in_brief(self):
        c = DuplicationCluster(
            concept="validate input",
            matches=[
                {"name": "fn_a", "file": "a.py", "module": "a", "similarity": 0.9},
                {"name": "fn_b", "file": "b.py", "module": "b", "similarity": 0.85},
                {"name": "fn_c", "file": "c.py", "module": "c", "similarity": 0.80},
            ],
        )
        brief = self._make(duplication_clusters=[c]).to_brief()
        assert "validate input" in brief
        assert "3" in brief  # file_spread

    def test_performance_signal_in_brief(self):
        from src.architecture_preflight import PerformanceStructureSignal
        sig = PerformanceStructureSignal(
            pattern="n_plus_one",
            structural_cause="Missing repository/batch layer",
            affected_files=["src/server.py"],
            count=2,
        )
        brief = self._make(performance_signals=[sig]).to_brief()
        assert "n_plus_one" in brief
        assert "repository" in brief.lower() or "batch" in brief.lower()

    def test_empty_preflight_has_no_none_placeholders(self):
        brief = self._make().to_brief()
        assert "None" not in brief
        assert "[]" not in brief


# ── run_preflight integration ─────────────────────────────────────────────────

class TestRunPreflight:
    def test_skips_perf_signals_when_findings_is_none(self):
        db = _DB()
        result = run(run_preflight(db, "proj", performance_findings=None, embeddings=None))
        assert result.performance_signals == []

    def test_skips_duplication_when_embeddings_is_none(self):
        db = _DB()
        result = run(run_preflight(db, "proj", performance_findings=[], embeddings=None))
        assert result.duplication_clusters == []

    def test_returns_preflight_dataclass(self):
        db = _DB()
        result = run(run_preflight(db, "proj"))
        assert isinstance(result, ArchitecturePreflight)
        assert result.project_id == "proj"
