"""
Tests for branch-aware conflict detection.

record_branch_changes  — log which functions a branch modified
get_branch_conflicts   — find overlapping changes across branches
index_changes fix      — branch no longer clobbered on incremental index

WHY THESE NEED TESTS
---------------------
Before this feature, all project records had an empty branch field because
index_changes called upsert_project without branch/head_commit, clobbering
the branch set by index_project. With multiple team members or agents indexing
the same project_id from different branches, the server is now the coordination
layer — it must reliably surface "who else touched this function."

The storage tests use a real Postgres DB (db fixture). The indexer tests use
a lightweight mock to test the _detect_git_context wiring without needing
a real git repo or DB connection.
"""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, patch
from src.branch_tracking import (
    MAIN_BRANCHES,
    BranchContext,
    classify_conflicts,
    empty_conflict_result,
)
from src.call_graph.storage import CallGraphDB


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _seed_project(db: CallGraphDB, project_id: str) -> None:
    await db.upsert_project(project_id, project_id, "/project")


# ══════════════════════════════════════════════════════════════════════════════
# Tracer bullet — record on one branch, see conflict from another
# ══════════════════════════════════════════════════════════════════════════════

class TestTracerBullet:
    """End-to-end: record change on branch-A, query from branch-B, get conflict back."""

    @pytest.mark.asyncio
    async def test_conflict_detected_across_branches(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/payments", ["src.auth.login"], "abc123")

        result = await db.get_branch_conflicts(project_id, ["src.auth.login"], current_branch="main")

        assert result["summary"]["total"] == 1
        conflict = result["conflicts"][0]
        assert conflict["function_id"] == "src.auth.login"
        assert conflict["competing_branches"][0]["branch"] == "feature/payments"


# ══════════════════════════════════════════════════════════════════════════════
# record_branch_changes
# ══════════════════════════════════════════════════════════════════════════════

class TestRecordBranchChanges:

    @pytest.mark.asyncio
    async def test_records_multiple_functions(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        fns = ["src.auth.login", "src.auth.logout", "src.auth.refresh"]
        await db.record_branch_changes(project_id, "feature/auth", fns, "deadbeef")

        result = await db.get_branch_conflicts(project_id, fns, current_branch="main")
        recorded = {c["function_id"] for c in result["conflicts"]}
        assert recorded == set(fns)

    @pytest.mark.asyncio
    async def test_upsert_does_not_duplicate(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/x", ["src.mod.fn"], "aaa")
        await db.record_branch_changes(project_id, "feature/x", ["src.mod.fn"], "bbb")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"], current_branch="main")
        branches = result["conflicts"][0]["competing_branches"]
        assert len(branches) == 1

    @pytest.mark.asyncio
    async def test_upsert_updates_head_commit(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/x", ["src.mod.fn"], "old_commit")
        await db.record_branch_changes(project_id, "feature/x", ["src.mod.fn"], "new_commit")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"], current_branch="main")
        touch = result["conflicts"][0]["competing_branches"][0]
        assert touch["head_commit"] == "new_commit"

    @pytest.mark.asyncio
    async def test_empty_branch_is_noop(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "", ["src.mod.fn"], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"])
        assert result["summary"]["total"] == 0

    @pytest.mark.asyncio
    async def test_empty_function_ids_is_noop(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/x", [], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"])
        assert result["summary"]["total"] == 0

    @pytest.mark.asyncio
    async def test_project_scoped(self, db: CallGraphDB, project_id: str):
        proj_a = f"{project_id}a"
        proj_b = f"{project_id}b"
        await _seed_project(db, proj_a)
        await _seed_project(db, proj_b)
        await db.record_branch_changes(proj_a, "feature/x", ["src.mod.fn"], "abc")

        result = await db.get_branch_conflicts(proj_b, ["src.mod.fn"])
        assert result["summary"]["total"] == 0


# ══════════════════════════════════════════════════════════════════════════════
# get_branch_conflicts
# ══════════════════════════════════════════════════════════════════════════════

class TestGetBranchConflicts:

    @pytest.mark.asyncio
    async def test_empty_when_no_changes_recorded(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"])
        assert result["conflicts"] == []
        assert result["summary"]["total"] == 0

    @pytest.mark.asyncio
    async def test_empty_function_ids_returns_empty(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        result = await db.get_branch_conflicts(project_id, [])
        assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_current_branch_excluded_from_results(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "my-branch", ["src.mod.fn"], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"], current_branch="my-branch")
        assert result["summary"]["total"] == 0

    @pytest.mark.asyncio
    async def test_no_current_branch_returns_all_branches(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/a", ["src.mod.fn"], "abc")
        await db.record_branch_changes(project_id, "feature/b", ["src.mod.fn"], "def")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"])
        branches = {t["branch"] for c in result["conflicts"] for t in c["competing_branches"]}
        assert {"feature/a", "feature/b"}.issubset(branches)

    @pytest.mark.asyncio
    async def test_main_drift_flagged_when_main_touched_function(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "main", ["src.auth.login"], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.auth.login"], current_branch="feature/x")
        conflict = result["conflicts"][0]
        assert conflict["main_drift"] is True
        assert "src.auth.login" in result["main_drift"]

    @pytest.mark.asyncio
    async def test_master_branch_also_flagged_as_main_drift(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "master", ["src.auth.login"], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.auth.login"], current_branch="feature/x")
        assert result["conflicts"][0]["main_drift"] is True

    @pytest.mark.asyncio
    async def test_non_main_branch_not_flagged_as_drift(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/payments", ["src.auth.login"], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.auth.login"], current_branch="feature/x")
        assert result["conflicts"][0]["main_drift"] is False
        assert result["main_drift"] == []

    @pytest.mark.asyncio
    async def test_multiple_competing_branches_for_same_function(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/a", ["src.mod.fn"], "aaa")
        await db.record_branch_changes(project_id, "feature/b", ["src.mod.fn"], "bbb")
        await db.record_branch_changes(project_id, "feature/c", ["src.mod.fn"], "ccc")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"], current_branch="feature/d")
        assert len(result["conflicts"]) == 1
        branches = {t["branch"] for t in result["conflicts"][0]["competing_branches"]}
        assert {"feature/a", "feature/b", "feature/c"}.issubset(branches)

    @pytest.mark.asyncio
    async def test_only_queried_functions_returned(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/x", ["src.a.fn", "src.b.fn"], "abc")

        result = await db.get_branch_conflicts(project_id, ["src.a.fn"], current_branch="main")
        fn_ids = {c["function_id"] for c in result["conflicts"]}
        assert fn_ids == {"src.a.fn"}

    @pytest.mark.asyncio
    async def test_summary_branches_list(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "feature/a", ["src.mod.fn"], "abc")
        await db.record_branch_changes(project_id, "feature/b", ["src.mod.fn"], "def")

        result = await db.get_branch_conflicts(project_id, ["src.mod.fn"], current_branch="main")
        assert {"feature/a", "feature/b"}.issubset(set(result["summary"]["branches"]))

    @pytest.mark.asyncio
    async def test_summary_functions_with_main_drift_count(self, db: CallGraphDB, project_id: str):
        await _seed_project(db, project_id)
        await db.record_branch_changes(project_id, "main", ["src.a.fn", "src.b.fn"], "abc")
        await db.record_branch_changes(project_id, "feature/x", ["src.c.fn"], "def")

        result = await db.get_branch_conflicts(
            project_id, ["src.a.fn", "src.b.fn", "src.c.fn"], current_branch="feature/y"
        )
        assert result["summary"]["functions_with_main_drift"] == 2


# ══════════════════════════════════════════════════════════════════════════════
# Indexer wiring — index_changes branch fix
# ══════════════════════════════════════════════════════════════════════════════

class TestIndexerBranchWiring:
    """
    Verify that index_changes calls _detect_git_context and passes branch
    to upsert_project (not clobbering with empty string), and that both
    index_project and index_changes call record_branch_changes.

    Uses mock DB/pipeline to avoid needing a real Postgres connection.
    """

    def _make_mock_db(self, branch_to_return="feature/x", head="abc123"):
        db = AsyncMock()
        db.get_nodes_by_file.return_value = []
        db.get_all_node_ids.return_value = set()
        db.upsert_nodes.return_value = None
        db.upsert_edges.return_value = None
        db.get_all_nodes.return_value = []
        db.record_branch_changes.return_value = None
        db.upsert_project.return_value = None
        db.list_external_dependencies.return_value = []
        db.get_latest_dependency_fingerprint.return_value = None
        db.save_dependency_fingerprint.return_value = None
        db.get_project_root.return_value = "/project"
        db.create_project_schema.return_value = None
        # project_db() returns the same mock — all configured methods apply to pdb too
        db.project_db.return_value = db
        return db

    def _make_mock_pipeline(self):
        from unittest.mock import MagicMock
        pipeline = AsyncMock()
        pipeline.delete_by_file.return_value = None
        pipeline.delete_by_ids.return_value = None
        pipeline.upsert_chunks.return_value = {"docs": 0, "fallback": 0}
        pipeline.get_embedded_ids.return_value = set()
        pipeline.get_summaries.return_value = {}
        # with_db is synchronous — override AsyncMock's default async treatment
        pipeline.with_db = MagicMock(return_value=pipeline)
        return pipeline

    @pytest.mark.asyncio
    async def test_index_changes_preserves_branch(self):
        """index_changes must detect git context and pass branch to upsert_project."""
        from src.indexer import Indexer
        from src.call_graph.parser import FunctionNode

        mock_db = self._make_mock_db()
        mock_pipeline = self._make_mock_pipeline()
        indexer = Indexer(mock_db, mock_pipeline)

        fn_node = FunctionNode(
            id="proj.mod.fn", name="fn",
            file="/project/src/mod.py", module="proj.mod",
            type="function", signature="def fn():", body="pass",
            docstring="", body_hash="newhash", is_external=False,
        )

        with patch("src.indexer.detect_branch", return_value=BranchContext("feature/x", "abc123")), \
             patch("src.indexer._parser.parse_file", return_value=([fn_node], [])):
            await indexer.index_changes(
                file_paths=["/project/src/mod.py"],
                file_contents={"/project/src/mod.py": "def fn(): pass"},
                project_root="/project",
                project_id="proj",
            )

        mock_db.upsert_project.assert_called_once()
        call_kwargs = mock_db.upsert_project.call_args
        args = call_kwargs[0]
        kwargs = call_kwargs[1]
        branch_passed = kwargs.get("branch") or (args[3] if len(args) > 3 else None)
        assert branch_passed == "feature/x", (
            "index_changes must pass the detected branch to upsert_project — "
            "previously it passed no branch, clobbering the stored value with ''."
        )

    @pytest.mark.asyncio
    async def test_index_changes_calls_record_branch_changes(self):
        """index_changes must populate branch_function_changes for changed functions."""
        from src.indexer import Indexer
        from src.call_graph.parser import FunctionNode

        mock_db = self._make_mock_db()
        mock_pipeline = self._make_mock_pipeline()
        indexer = Indexer(mock_db, mock_pipeline)

        fn_node = FunctionNode(
            id="proj.mod.my_func", name="my_func",
            file="/project/src/mod.py", module="proj.mod",
            type="function", signature="def my_func():", body="pass",
            docstring="", body_hash="new_hash", is_external=False,
        )

        with patch("src.indexer.detect_branch", return_value=BranchContext("feature/x", "abc123")), \
             patch("src.indexer._parser.parse_file", return_value=([fn_node], [])):
            await indexer.index_changes(
                file_paths=["/project/src/mod.py"],
                file_contents={"/project/src/mod.py": "def my_func(): pass"},
                project_root="/project",
                project_id="proj",
            )

        mock_db.record_branch_changes.assert_called_once()
        call_args = mock_db.record_branch_changes.call_args[0]
        assert call_args[1] == "feature/x"   # branch
        assert "proj.mod.my_func" in call_args[2]  # function_ids

    @pytest.mark.asyncio
    async def test_index_changes_no_record_when_nothing_changed(self):
        """record_branch_changes must not be called when no functions changed."""
        from src.indexer import Indexer

        mock_db = self._make_mock_db()
        mock_pipeline = self._make_mock_pipeline()
        indexer = Indexer(mock_db, mock_pipeline)

        with patch("src.indexer.detect_branch", return_value=BranchContext("feature/x", "abc123")), \
             patch("src.indexer._parser.parse_file", return_value=([], [])):
            await indexer.index_changes(
                file_paths=["/project/src/mod.py"],
                file_contents={"/project/src/mod.py": "def fn(): pass"},
                project_root="/project",
                project_id="proj",
            )

        mock_db.record_branch_changes.assert_not_called()


# ══════════════════════════════════════════════════════════════════════════════
# classify_conflicts — pure function tests (no DB, no async)
# ══════════════════════════════════════════════════════════════════════════════

class TestClassifyConflicts:
    """
    classify_conflicts is the domain logic extracted from storage.get_branch_conflicts.
    All branch semantics (MAIN_BRANCHES, grouping, drift flag) live here and are
    testable without any database connection.
    """

    def _row(self, branch: str, fn_id: str, head: str = "abc", ts: str = "2026-01-01T00:00:00") -> dict:
        return {"branch": branch, "function_id": fn_id, "head_commit": head, "modified_at": ts}

    def test_empty_rows_returns_empty_result(self):
        result = classify_conflicts([])
        assert result["conflicts"] == []
        assert result["main_drift"] == []
        assert result["summary"]["total"] == 0

    def test_single_row_produces_one_conflict(self):
        rows = [self._row("feature/x", "src.auth.login")]
        result = classify_conflicts(rows)
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["function_id"] == "src.auth.login"

    def test_groups_multiple_branches_under_same_function(self):
        rows = [
            self._row("feature/a", "src.mod.fn"),
            self._row("feature/b", "src.mod.fn"),
        ]
        result = classify_conflicts(rows)
        assert len(result["conflicts"]) == 1
        branches = {t["branch"] for t in result["conflicts"][0]["competing_branches"]}
        assert branches == {"feature/a", "feature/b"}

    def test_keeps_multiple_functions_separate(self):
        rows = [
            self._row("feature/a", "src.mod.fn1"),
            self._row("feature/a", "src.mod.fn2"),
        ]
        result = classify_conflicts(rows)
        fn_ids = {c["function_id"] for c in result["conflicts"]}
        assert fn_ids == {"src.mod.fn1", "src.mod.fn2"}

    def test_main_branch_sets_drift_flag(self):
        rows = [self._row("main", "src.auth.login")]
        result = classify_conflicts(rows)
        assert result["conflicts"][0]["main_drift"] is True
        assert "src.auth.login" in result["main_drift"]

    def test_master_branch_also_sets_drift_flag(self):
        rows = [self._row("master", "src.auth.login")]
        result = classify_conflicts(rows)
        assert result["conflicts"][0]["main_drift"] is True

    def test_non_main_branch_does_not_set_drift(self):
        rows = [self._row("feature/payments", "src.auth.login")]
        result = classify_conflicts(rows)
        assert result["conflicts"][0]["main_drift"] is False
        assert result["main_drift"] == []

    def test_summary_branches_sorted(self):
        rows = [
            self._row("feature/z", "src.mod.fn"),
            self._row("feature/a", "src.mod.fn"),
        ]
        result = classify_conflicts(rows)
        assert result["summary"]["branches"] == ["feature/a", "feature/z"]

    def test_summary_functions_with_main_drift_count(self):
        rows = [
            self._row("main", "src.a.fn"),
            self._row("main", "src.b.fn"),
            self._row("feature/x", "src.c.fn"),
        ]
        result = classify_conflicts(rows)
        assert result["summary"]["functions_with_main_drift"] == 2

    def test_main_branches_constant_is_the_authority(self):
        """MAIN_BRANCHES drives drift detection — one place to extend."""
        assert "main" in MAIN_BRANCHES
        assert "master" in MAIN_BRANCHES

    def test_empty_conflict_result_shape_matches_classify_output(self):
        """empty_conflict_result and classify_conflicts([]) must have the same keys."""
        empty = empty_conflict_result()
        from_classify = classify_conflicts([])
        assert set(empty.keys()) == set(from_classify.keys())
        assert set(empty["summary"].keys()) == set(from_classify["summary"].keys())
