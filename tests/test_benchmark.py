"""
Tests for the benchmark module.

Covers: output parsing, prompt builders, calibration selection, patch saving.
All tests use public interfaces only — no subprocess, no network, no git.
"""
import json
import textwrap
from pathlib import Path

import pytest

from benchmark.loader import BenchmarkTask, select_calibration_tasks
from benchmark.runner import build_prompt_a, build_prompt_b, save_patch
from benchmark.repo_setup import RepoContext
from benchmark.evaluator import _parse_pytest_output


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _task(**kwargs) -> BenchmarkTask:
    defaults = dict(
        instance_id="pytest-dev__pytest-1234",
        repo="pytest-dev/pytest",
        base_commit="abc123def456",
        problem_statement="Something is broken.",
        fail_to_pass=["testing/test_foo.py::test_bar"],
        pass_to_pass=[],
    )
    return BenchmarkTask(**{**defaults, **kwargs})


def _ctx(task=None, **kwargs) -> RepoContext:
    t = task or _task()
    defaults = dict(
        task=t,
        repo_path="/tmp/bench/pytest-dev__pytest-1234",
        venv_python="/tmp/bench/pytest-dev__pytest-1234/.bench-venv/bin/python",
        project_id="bench-pytest-dev__pytest-1234",
        phronosis_indexed=True,
    )
    return RepoContext(**{**defaults, **kwargs})


# ── _parse_pytest_output (tracer bullet — this is the broken thing) ──────────

_parse = _parse_pytest_output  # alias for readability in tests


VERBOSE_OUTPUT = textwrap.dedent("""\
    testing/test_foo.py::test_bar PASSED                     [ 33%]
    testing/test_foo.py::test_baz PASSED                     [ 66%]
    testing/test_foo.py::test_fail FAILED                    [100%]

    =========================== short test summary info ============================
    FAILED testing/test_foo.py::test_fail - assert 1 == 2
    ========================= 1 failed, 2 passed in 0.01s ==========================
""")

QUIET_OUTPUT = textwrap.dedent("""\
    ..F                                                                      [100%]
    =========================== short test summary info ============================
    FAILED testing/test_foo.py::test_fail - assert 1 == 2
    1 failed, 2 passed in 0.01s
""")


def test_parse_verbose_detects_passed():
    ids = ["testing/test_foo.py::test_bar", "testing/test_foo.py::test_baz", "testing/test_foo.py::test_fail"]
    passed, _ = _parse(VERBOSE_OUTPUT, ids)
    assert "testing/test_foo.py::test_bar" in passed
    assert "testing/test_foo.py::test_baz" in passed


def test_parse_verbose_detects_failed():
    ids = ["testing/test_foo.py::test_bar", "testing/test_foo.py::test_baz", "testing/test_foo.py::test_fail"]
    _, failed = _parse(VERBOSE_OUTPUT, ids)
    assert "testing/test_foo.py::test_fail" in failed


def test_parse_verbose_passed_not_in_failed():
    ids = ["testing/test_foo.py::test_bar", "testing/test_foo.py::test_fail"]
    passed, failed = _parse(VERBOSE_OUTPUT, ids)
    assert "testing/test_foo.py::test_bar" not in failed
    assert "testing/test_foo.py::test_fail" not in passed


def test_parse_quiet_detects_failed_from_summary():
    ids = ["testing/test_foo.py::test_bar", "testing/test_foo.py::test_fail"]
    _, failed = _parse(QUIET_OUTPUT, ids)
    assert "testing/test_foo.py::test_fail" in failed


def test_parse_unseen_test_marked_failed():
    ids = ["testing/test_foo.py::test_missing"]
    _, failed = _parse("", ids)
    assert "testing/test_foo.py::test_missing" in failed


def test_parse_empty_output_all_failed():
    ids = ["testing/test_foo.py::test_a", "testing/test_foo.py::test_b"]
    passed, failed = _parse("", ids)
    assert passed == []
    assert set(failed) == set(ids)


# ── evaluator.evaluate — no-patch fast path ───────────────────────────────────

def test_evaluate_no_patch_returns_unresolved():
    from benchmark.evaluator import evaluate
    from benchmark.runner import AgentResult

    task = _task()
    agent = AgentResult(
        instance_id=task.instance_id,
        path="a",
        patch="",
        tool_calls=[],
        iterations=1,
        submitted=False,
    )
    result = evaluate(task, agent, repo_path="/nonexistent", venv_python="python")
    assert result.resolved is False
    assert result.patch_applied is False
    assert result.error is not None


# ── prompt builders ───────────────────────────────────────────────────────────

def test_prompt_a_prohibits_phronosis():
    pa = build_prompt_a(_task(), _ctx())
    assert "do NOT" in pa or "Do NOT" in pa
    assert "Phronosis" in pa


def test_prompt_a_contains_venv_path():
    ctx = _ctx()
    pa = build_prompt_a(_task(), ctx)
    assert ctx.venv_python in pa


def test_prompt_a_contains_fail_to_pass_tests():
    task = _task(fail_to_pass=["testing/test_foo.py::test_bar", "testing/test_foo.py::test_baz"])
    pa = build_prompt_a(task, _ctx(task=task))
    assert "testing/test_foo.py::test_bar" in pa
    assert "testing/test_foo.py::test_baz" in pa


def test_prompt_b_instructs_phronosis_use():
    pb = build_prompt_b(_task(), _ctx())
    assert "Phronosis" in pb
    assert "get_project_home" in pb


def test_prompt_b_contains_project_id():
    ctx = _ctx(project_id="bench-pytest-dev__pytest-9999")
    pb = build_prompt_b(_task(), ctx)
    assert "bench-pytest-dev__pytest-9999" in pb


def test_prompt_b_contains_repo_path():
    ctx = _ctx(repo_path="/tmp/some/special/path")
    pb = build_prompt_b(_task(), ctx)
    assert "/tmp/some/special/path" in pb


def test_prompt_a_and_b_differ():
    task = _task()
    ctx = _ctx(task=task)
    assert build_prompt_a(task, ctx) != build_prompt_b(task, ctx)


# ── select_calibration_tasks ──────────────────────────────────────────────────

def _make_tasks(counts: list[int]) -> list[BenchmarkTask]:
    return [
        BenchmarkTask(
            instance_id=f"repo__issue-{i}",
            repo="r/r",
            base_commit=f"commit{i}",
            problem_statement="p",
            fail_to_pass=[f"t{j}" for j in range(n)],
            pass_to_pass=[],
        )
        for i, n in enumerate(counts)
    ]


def test_calibration_returns_correct_count():
    tasks = _make_tasks([1, 3, 2, 5, 1, 4])
    result = select_calibration_tasks(tasks, n_hard=3, n_random=2)
    assert len(result) == 5


def test_calibration_hard_tasks_have_most_tests():
    tasks = _make_tasks([1, 5, 2, 4, 3])
    result = select_calibration_tasks(tasks, n_hard=2, n_random=1)
    hard = result[:2]
    hard_counts = [len(t.fail_to_pass) for t in hard]
    assert 5 in hard_counts
    assert 4 in hard_counts


def test_calibration_random_from_remainder():
    tasks = _make_tasks([1, 5, 2, 4, 3])
    result = select_calibration_tasks(tasks, n_hard=2, n_random=2)
    hard_ids = {t.instance_id for t in result[:2]}
    random_ids = {t.instance_id for t in result[2:]}
    assert hard_ids & random_ids == set()  # no overlap


def test_calibration_is_deterministic():
    tasks = _make_tasks([1, 3, 2, 5, 1, 4, 2, 3])
    r1 = select_calibration_tasks(tasks, n_hard=3, n_random=2)
    r2 = select_calibration_tasks(tasks, n_hard=3, n_random=2)
    assert [t.instance_id for t in r1] == [t.instance_id for t in r2]


# ── save_patch ────────────────────────────────────────────────────────────────

def test_save_patch_writes_to_correct_path(tmp_path):
    patch = "diff --git a/foo.py b/foo.py\n--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
    out = save_patch(patch, str(tmp_path), "pytest-dev__pytest-1234", "b")
    assert out == tmp_path / "pytest-dev__pytest-1234" / "path_b" / "patch.diff"
    assert out.read_text() == patch


def test_save_patch_creates_directories(tmp_path):
    save_patch("some diff", str(tmp_path), "pytest-dev__pytest-9999", "a")
    assert (tmp_path / "pytest-dev__pytest-9999" / "path_a" / "patch.diff").exists()


def test_save_patch_empty_patch(tmp_path):
    out = save_patch("", str(tmp_path), "pytest-dev__pytest-1234", "a")
    assert out.read_text() == ""


# ── repo_setup helpers ────────────────────────────────────────────────────────

def test_base_clone_root_env_respected(monkeypatch, tmp_path):
    """BENCH_CLONE_ROOT env var controls where base clones land."""
    import benchmark.repo_setup as rs
    monkeypatch.setenv("BENCH_CLONE_ROOT", str(tmp_path / "clones"))
    from importlib import reload
    reload(rs)
    assert str(tmp_path / "clones") in str(rs._BASE_CLONE_ROOT)


def test_indexed_commits_cache_skips_reindex(monkeypatch):
    """If a commit was already indexed this session, _ensure_indexed returns early."""
    import benchmark.repo_setup as rs
    rs._indexed_commits["deadbeef"] = "bench-some-task"

    calls = []
    original = rs.urllib.request.urlopen
    def fake_urlopen(*a, **kw):
        calls.append(a)
        return original(*a, **kw)

    task = _task(base_commit="deadbeef")
    result = rs._ensure_indexed(task, "/tmp/fake-repo", "/tmp/fake-base-clone")
    assert result == "bench-some-task"
    assert calls == []  # no HTTP call made
