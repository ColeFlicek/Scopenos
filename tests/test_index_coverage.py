"""
Tests for IndexCoverage — the post-commit audit dataclass.

All tests construct IndexCoverage directly with plain values.
No database, no async. The DB queries that populate it are tested
by integration tests (future); here we test the logic that turns
raw counts into status, gap, and recommendation.

Each class maps to one of the three failure modes the coverage check detects:
  - partial:   some nodes have no vector at all
  - degraded:  all nodes have vectors but some were embedded from empty content
  - ok:        every node has a meaningful vector
"""
from src.index_coverage import IndexCoverage


def _coverage(**kwargs) -> IndexCoverage:
    """Build an IndexCoverage with sensible defaults, overriding with kwargs."""
    defaults = dict(
        project_id="test",
        expected=10,
        actual=10,
        missing_vectors=[],
        degraded_count=0,
        on_large_model=0,
    )
    return IndexCoverage(**{**defaults, **kwargs})


# ── status ────────────────────────────────────────────────────────────────────

class TestStatus:
    def test_ok_when_everything_clean(self):
        c = _coverage(expected=10, actual=10, missing_vectors=[], degraded_count=0)
        assert c.status == "ok"

    def test_partial_when_vectors_missing(self):
        c = _coverage(missing_vectors=["src.server.fn_a", "src.server.fn_b"])
        assert c.status == "partial"

    def test_partial_takes_priority_over_degraded(self):
        # If vectors are missing AND content is degraded, partial wins.
        c = _coverage(missing_vectors=["src.server.fn"], degraded_count=5)
        assert c.status == "partial"

    def test_degraded_when_missing_is_empty_but_content_null(self):
        c = _coverage(missing_vectors=[], degraded_count=3)
        assert c.status == "degraded"

    def test_ok_when_only_large_model_fallback(self):
        # Large-model fallback is a quality issue, not a status issue.
        c = _coverage(on_large_model=50)
        assert c.status == "ok"


# ── gap ───────────────────────────────────────────────────────────────────────

class TestGap:
    def test_gap_is_zero_when_no_missing(self):
        c = _coverage(missing_vectors=[])
        assert c.gap == 0

    def test_gap_equals_missing_vector_count(self):
        missing = [f"src.module.fn_{i}" for i in range(47)]
        c = _coverage(missing_vectors=missing)
        assert c.gap == 47

    def test_gap_reflects_partial_failure_scenario(self):
        """200 nodes indexed, 100 vectors written — gap is 100."""
        missing = [f"fn_{i}" for i in range(100)]
        c = _coverage(expected=200, actual=100, missing_vectors=missing)
        assert c.gap == 100


# ── recommendation ────────────────────────────────────────────────────────────

class TestRecommendation:
    def test_none_when_ok(self):
        c = _coverage()
        assert c.recommendation is None

    def test_reembed_recommended_for_missing_vectors(self):
        c = _coverage(project_id="ACIP", missing_vectors=["fn_a", "fn_b"])
        assert "reembed_project('ACIP')" in c.recommendation
        assert "2" in c.recommendation

    def test_enrich_recommended_for_degraded(self):
        c = _coverage(project_id="ACIP", degraded_count=15)
        assert "enrich_summaries('ACIP')" in c.recommendation
        assert "15" in c.recommendation

    def test_enrich_recommended_for_large_model(self):
        c = _coverage(project_id="ACIP", on_large_model=30)
        assert "enrich_summaries('ACIP')" in c.recommendation
        assert "30" in c.recommendation

    def test_reembed_takes_priority_over_enrich(self):
        # If vectors are missing, recommend reembed first — more urgent than quality.
        c = _coverage(
            project_id="ACIP",
            missing_vectors=["fn_a"],
            degraded_count=10,
            on_large_model=50,
        )
        assert "reembed_project" in c.recommendation
        assert "enrich_summaries" not in c.recommendation

    def test_degraded_takes_priority_over_large_model(self):
        c = _coverage(project_id="ACIP", degraded_count=5, on_large_model=50)
        assert "enrich_summaries" in c.recommendation
        assert "5" in c.recommendation


# ── as_dict ───────────────────────────────────────────────────────────────────

class TestAsDict:
    def test_ok_dict_has_no_recommendation_key(self):
        d = _coverage().as_dict()
        assert "recommendation" not in d

    def test_partial_dict_includes_recommendation(self):
        d = _coverage(missing_vectors=["fn_a"]).as_dict()
        assert "recommendation" in d

    def test_missing_vector_ids_only_present_when_nonempty(self):
        d_ok = _coverage(missing_vectors=[]).as_dict()
        assert "missing_vector_ids" not in d_ok

        d_partial = _coverage(missing_vectors=["fn_a"]).as_dict()
        assert "missing_vector_ids" in d_partial
        assert d_partial["missing_vector_ids"] == ["fn_a"]

    def test_dict_always_contains_core_fields(self):
        d = _coverage(expected=10, actual=8, missing_vectors=["fn_a", "fn_b"]).as_dict()
        assert d["status"] == "partial"
        assert d["expected"] == 10
        assert d["actual"] == 8
        assert d["gap"] == 2
        assert d["degraded"] == 0

    def test_dict_is_json_serialisable(self):
        import json
        d = _coverage(
            missing_vectors=["fn_a"],
            degraded_count=3,
            on_large_model=10,
        ).as_dict()
        # Should not raise
        json.dumps(d)


# ── end-to-end scenario ───────────────────────────────────────────────────────

class TestScenarios:
    def test_fresh_project_first_index_success(self):
        """All 50 functions indexed and embedded — clean first run."""
        c = _coverage(expected=50, actual=50, missing_vectors=[], degraded_count=0)
        assert c.status == "ok"
        assert c.gap == 0
        assert c.recommendation is None

    def test_embedding_api_partial_failure(self):
        """
        200 functions parsed, call graph committed, then embedding API timed out
        after 150 functions. 50 have no vector. This is the scenario that motivated
        the coverage check.
        """
        missing = [f"src.module.fn_{i}" for i in range(50)]
        c = _coverage(
            project_id="ACIP",
            expected=200,
            actual=150,
            missing_vectors=missing,
        )
        assert c.status == "partial"
        assert c.gap == 50
        assert "reembed_project" in c.recommendation

    def test_all_functions_have_vectors_but_all_undocumented(self):
        """
        A project with no docstrings anywhere. Every function got embedded via the
        large-model fallback. Vectors exist but are low-quality semantic signal.
        """
        c = _coverage(
            project_id="ACIP",
            expected=100,
            actual=100,
            missing_vectors=[],
            degraded_count=0,
            on_large_model=100,
        )
        assert c.status == "ok"   # not broken — just quality-improvable
        assert "enrich_summaries" in c.recommendation

    def test_partial_docstring_coverage(self):
        """
        50 documented functions (small model, good vectors) +
        30 undocumented (large model, raw code vectors) +
        20 that slipped through with no vector at all.
        """
        missing = [f"fn_{i}" for i in range(20)]
        c = _coverage(
            project_id="ACIP",
            expected=100,
            actual=80,
            missing_vectors=missing,
            degraded_count=0,
            on_large_model=30,
        )
        assert c.status == "partial"
        assert c.gap == 20
        assert "reembed_project" in c.recommendation
