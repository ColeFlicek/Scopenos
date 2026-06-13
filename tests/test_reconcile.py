"""
Tests for reconcile() and IndexDelta.

These are the most important tests in the project for indexing correctness.
Every case here maps directly to something that can go wrong in production:
a function that doesn't get re-embedded after a body change, a deleted function
whose stale vector lingers, a coverage gap the self-verification would catch.

All tests use plain dicts — no database, no parser, no async.
"""
from src.index_delta import IndexDelta, reconcile


# ── New functions ─────────────────────────────────────────────────────────────

class TestNewFunctions:
    def test_new_function_goes_into_to_embed(self):
        delta = reconcile({}, {"src.server.fn": "hash1"})
        assert "src.server.fn" in delta.to_embed

    def test_new_function_not_in_to_delete(self):
        # There's no old vector to clean up for a brand-new function.
        delta = reconcile({}, {"src.server.fn": "hash1"})
        assert "src.server.fn" not in delta.to_delete

    def test_new_function_not_in_unchanged(self):
        delta = reconcile({}, {"src.server.fn": "hash1"})
        assert "src.server.fn" not in delta.unchanged

    def test_new_function_appears_in_functions_added(self):
        delta = reconcile({}, {"src.server.fn": "hash1"})
        assert "src.server.fn" in delta.functions_added

    def test_multiple_new_functions(self):
        delta = reconcile({}, {"fn_a": "h1", "fn_b": "h2", "fn_c": "h3"})
        assert delta.to_embed == frozenset({"fn_a", "fn_b", "fn_c"})
        assert delta.to_delete == frozenset()
        assert delta.unchanged == frozenset()


# ── Deleted functions ─────────────────────────────────────────────────────────

class TestDeletedFunctions:
    def test_deleted_function_goes_into_to_delete(self):
        delta = reconcile({"src.server.fn": "hash1"}, {})
        assert "src.server.fn" in delta.to_delete

    def test_deleted_function_not_in_to_embed(self):
        # A removed function should not be re-embedded.
        delta = reconcile({"src.server.fn": "hash1"}, {})
        assert "src.server.fn" not in delta.to_embed

    def test_deleted_function_not_in_unchanged(self):
        delta = reconcile({"src.server.fn": "hash1"}, {})
        assert "src.server.fn" not in delta.unchanged

    def test_deleted_function_appears_in_functions_removed(self):
        delta = reconcile({"src.server.fn": "hash1"}, {})
        assert "src.server.fn" in delta.functions_removed

    def test_deleting_all_functions(self):
        delta = reconcile({"fn_a": "h1", "fn_b": "h2"}, {})
        assert delta.to_delete == frozenset({"fn_a", "fn_b"})
        assert delta.to_embed == frozenset()
        assert delta.unchanged == frozenset()


# ── Changed functions ─────────────────────────────────────────────────────────

class TestChangedFunctions:
    def test_changed_body_goes_into_to_embed(self):
        delta = reconcile({"fn_a": "old_hash"}, {"fn_a": "new_hash"})
        assert "fn_a" in delta.to_embed

    def test_changed_body_also_in_to_delete(self):
        # The old vector is stale and must be removed before the new one is written.
        delta = reconcile({"fn_a": "old_hash"}, {"fn_a": "new_hash"})
        assert "fn_a" in delta.to_delete

    def test_changed_function_not_in_unchanged(self):
        delta = reconcile({"fn_a": "old_hash"}, {"fn_a": "new_hash"})
        assert "fn_a" not in delta.unchanged

    def test_changed_function_appears_in_functions_changed(self):
        delta = reconcile({"fn_a": "old_hash"}, {"fn_a": "new_hash"})
        assert "fn_a" in delta.functions_changed

    def test_changed_function_not_in_functions_added_or_removed(self):
        delta = reconcile({"fn_a": "old_hash"}, {"fn_a": "new_hash"})
        assert "fn_a" not in delta.functions_added
        assert "fn_a" not in delta.functions_removed


# ── Unchanged functions ───────────────────────────────────────────────────────

class TestUnchangedFunctions:
    def test_same_hash_goes_into_unchanged(self):
        delta = reconcile({"fn_a": "hash1"}, {"fn_a": "hash1"})
        assert "fn_a" in delta.unchanged

    def test_unchanged_not_in_to_embed(self):
        # Don't waste an embedding API call on a function that didn't change.
        delta = reconcile({"fn_a": "hash1"}, {"fn_a": "hash1"})
        assert "fn_a" not in delta.to_embed

    def test_unchanged_not_in_to_delete(self):
        # Don't delete a valid existing vector.
        delta = reconcile({"fn_a": "hash1"}, {"fn_a": "hash1"})
        assert "fn_a" not in delta.to_delete


# ── Mixed scenarios ───────────────────────────────────────────────────────────

class TestMixedScenarios:
    def test_real_world_incremental_update(self):
        """
        Typical incremental index: one function added, one changed, one removed,
        two unchanged. This is the scenario that runs on every file save.
        """
        old = {
            "src.server.stable_a": "hash_a",
            "src.server.stable_b": "hash_b",
            "src.server.will_change": "hash_old",
            "src.server.will_delete": "hash_d",
        }
        new = {
            "src.server.stable_a": "hash_a",      # unchanged
            "src.server.stable_b": "hash_b",      # unchanged
            "src.server.will_change": "hash_new",  # changed
            "src.server.brand_new": "hash_n",      # added
            # will_delete is gone
        }
        delta = reconcile(old, new)

        assert delta.unchanged == frozenset({"src.server.stable_a", "src.server.stable_b"})
        assert "src.server.will_change" in delta.to_embed
        assert "src.server.will_change" in delta.to_delete
        assert "src.server.brand_new" in delta.to_embed
        assert "src.server.brand_new" not in delta.to_delete
        assert "src.server.will_delete" in delta.to_delete
        assert "src.server.will_delete" not in delta.to_embed

    def test_empty_old_and_new(self):
        delta = reconcile({}, {})
        assert delta.to_embed == frozenset()
        assert delta.to_delete == frozenset()
        assert delta.unchanged == frozenset()

    def test_no_changes_at_all(self):
        hashes = {"fn_a": "h1", "fn_b": "h2", "fn_c": "h3"}
        delta = reconcile(hashes, hashes)
        assert delta.to_embed == frozenset()
        assert delta.to_delete == frozenset()
        assert delta.unchanged == frozenset(hashes.keys())

    def test_complete_replacement(self):
        """All old functions removed, all new functions added — full re-index."""
        old = {"fn_old_a": "h1", "fn_old_b": "h2"}
        new = {"fn_new_a": "h3", "fn_new_b": "h4"}
        delta = reconcile(old, new)
        assert delta.to_embed == frozenset(new.keys())
        assert delta.to_delete == frozenset(old.keys())
        assert delta.unchanged == frozenset()


# ── expected_vector_count ─────────────────────────────────────────────────────

class TestExpectedVectorCount:
    def test_count_equals_to_embed_plus_unchanged(self):
        """
        This is the invariant the coverage check enforces.
        After a successful commit, every node in to_embed | unchanged must have a vector.
        """
        old = {"stable": "h1", "changed": "h2", "deleted": "h3"}
        new = {"stable": "h1", "changed": "h_new", "brand_new": "h4"}
        delta = reconcile(old, new)

        # stable=unchanged, changed=to_embed, brand_new=to_embed → 3 expected
        assert delta.expected_vector_count == 3

    def test_count_is_zero_for_empty_index(self):
        delta = reconcile({}, {})
        assert delta.expected_vector_count == 0

    def test_count_equals_total_nodes_when_all_new(self):
        new = {"fn_a": "h1", "fn_b": "h2", "fn_c": "h3"}
        delta = reconcile({}, new)
        assert delta.expected_vector_count == len(new)

    def test_count_equals_total_nodes_when_nothing_changed(self):
        hashes = {"fn_a": "h1", "fn_b": "h2"}
        delta = reconcile(hashes, hashes)
        assert delta.expected_vector_count == len(hashes)

    def test_count_is_zero_when_all_deleted(self):
        old = {"fn_a": "h1", "fn_b": "h2"}
        delta = reconcile(old, {})
        assert delta.expected_vector_count == 0

    def test_coverage_gap_scenario(self):
        """
        Simulate the failure mode: 200 functions parsed, only 100 get written.
        expected_vector_count = 200, but after a partial failure the DB has 100.
        The gap is 100. This is what the coverage check catches.
        """
        old = {}
        new = {f"src.module.fn_{i}": f"hash_{i}" for i in range(200)}
        delta = reconcile(old, new)
        assert delta.expected_vector_count == 200
        # Simulate: only 100 vectors actually written (e.g., embedding API timed out)
        vectors_actually_written = 100
        gap = delta.expected_vector_count - vectors_actually_written
        assert gap == 100


# ── Disjointness invariant ────────────────────────────────────────────────────

class TestDisjointness:
    """
    A function ID must appear in exactly one of {to_embed, unchanged}.
    This invariant ensures the Commit phase doesn't double-embed or skip anything.
    """
    def test_to_embed_and_unchanged_are_disjoint(self):
        old = {"a": "h1", "b": "h2", "c": "h3"}
        new = {"a": "h1", "b": "h_changed", "d": "h4"}
        delta = reconcile(old, new)
        assert delta.to_embed & delta.unchanged == frozenset()

    def test_all_new_functions_accounted_for(self):
        """Every function in new_hashes ends up in exactly one bucket."""
        old = {"a": "h1"}
        new = {"a": "h1", "b": "h_new", "c": "h_changed_from_nothing"}
        delta = reconcile(old, new)
        all_new_ids = set(new.keys())
        covered = delta.to_embed | delta.unchanged
        assert covered == all_new_ids
