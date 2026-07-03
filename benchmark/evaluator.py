"""
Apply an agent's patch to the repo and run the fail_to_pass tests.

This is the verifiable ground truth — the result can be independently
confirmed by anyone with the repo, the patch, and a Python install.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .loader import BenchmarkTask
from .runner import AgentResult


@dataclass
class EvaluationResult:
    instance_id: str
    path: str                        # "a" or "b"
    patch_applied: bool
    tests_passed: list[str]
    tests_failed: list[str]
    error: str | None = None

    @property
    def resolved(self) -> bool:
        """True if ALL fail_to_pass tests now pass."""
        return self.patch_applied and not self.tests_failed


def evaluate(
    task: BenchmarkTask,
    agent_result: AgentResult,
    repo_path: str,
    venv_python: str = "python",
) -> EvaluationResult:
    """
    Apply agent_result.patch to a clean checkout and run fail_to_pass tests.

    repo_path must be the same checkout used during the agent run (same base_commit).
    The evaluation runs in a temp copy so the original checkout is not mutated.
    """
    if not agent_result.patch.strip():
        return EvaluationResult(
            instance_id=task.instance_id,
            path=agent_result.path,
            patch_applied=False,
            tests_passed=[],
            tests_failed=list(task.fail_to_pass),
            error="Agent produced no patch (no files modified)",
        )

    with tempfile.TemporaryDirectory(prefix="scopenos-eval-") as tmpdir:
        # Copy the checkout into a fresh directory for evaluation, then reset to
        # HEAD so the patch can be applied cleanly (the worktree may already
        # contain the agent's uncommitted changes and untracked helper files).
        eval_path = os.path.join(tmpdir, "repo")
        subprocess.run(["cp", "-r", repo_path, eval_path], check=True)
        subprocess.run(["git", "checkout", "--", "."], cwd=eval_path, check=True)
        subprocess.run(["git", "clean", "-fd"], cwd=eval_path, check=True)

        # Write patch to a temp file and apply it
        patch_file = os.path.join(tmpdir, "agent.patch")
        Path(patch_file).write_text(agent_result.patch, encoding="utf-8")

        apply = subprocess.run(
            ["git", "apply", "--check", patch_file],
            cwd=eval_path,
            capture_output=True,
        )
        if apply.returncode != 0:
            return EvaluationResult(
                instance_id=task.instance_id,
                path=agent_result.path,
                patch_applied=False,
                tests_passed=[],
                tests_failed=list(task.fail_to_pass),
                error=f"Patch does not apply cleanly: {apply.stderr.decode()[:500]}",
            )

        subprocess.run(["git", "apply", patch_file], cwd=eval_path, check=True)

        # Run only the fail_to_pass tests — fast and focused
        passed, failed = _run_tests(task.fail_to_pass, eval_path, venv_python)

    return EvaluationResult(
        instance_id=task.instance_id,
        path=agent_result.path,
        patch_applied=True,
        tests_passed=passed,
        tests_failed=failed,
    )


def _run_tests(test_ids: list[str], repo_path: str, python: str = "python") -> tuple[list[str], list[str]]:
    """Run a list of test IDs and return (passed, failed).

    Handles both pytest node IDs and Django's 'test_name (module.ClassName)' format.
    """
    if not test_ids:
        return [], []

    if _is_django_format(test_ids[0]):
        return _run_django_tests(test_ids, repo_path, python)

    result = subprocess.run(
        [python, "-m", "pytest", "-v", "--tb=no", *test_ids],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=300,
    )

    return _parse_pytest_output(result.stdout + result.stderr, test_ids)


def _is_django_format(test_id: str) -> bool:
    """Detect Django's 'test_name (module.ClassName)' test ID format."""
    return " (" in test_id and test_id.endswith(")")


def _run_django_tests(test_ids: list[str], repo_path: str, python: str) -> tuple[list[str], list[str]]:
    """Run Django-format tests using runtests.py or pytest-django."""
    passed, failed = [], []
    tests_dir = os.path.join(repo_path, "tests")
    runtests = os.path.join(tests_dir, "runtests.py")

    for tid in test_ids:
        # Docstring-style test ID: 'Subquery annotations are excluded from...'
        # These aren't runnable by name — run the module and search output for the docstring
        if not _is_django_format(tid) and not tid.startswith("test_"):
            result_p, result_f = _run_django_docstring_test(tid, test_ids, repo_path, python)
            passed.extend(result_p)
            failed.extend(result_f)
            continue

        dotted = _django_id_to_dotted(tid)
        if os.path.exists(runtests):
            # PYTHONPATH ensures Django's source tree takes precedence over any
            # installed Django in the venv — needed when venv Django != source Django.
            env = {**os.environ, "PYTHONPATH": repo_path}
            result = subprocess.run(
                [python, "runtests.py", "--verbosity=2", "--parallel=1", dotted],
                cwd=tests_dir,
                capture_output=True,
                text=True,
                timeout=120,
                env=env,
            )
        else:
            pytest_id = _django_id_to_pytest(tid, repo_path)
            result = subprocess.run(
                [python, "-m", "pytest", "-v", "--tb=no", pytest_id],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=120,
            )
        output = result.stdout + result.stderr
        if "OK" in output or " PASSED" in output or "... ok" in output:
            passed.append(tid)
        else:
            failed.append(tid)

    return passed, failed


def _run_django_docstring_test(
    docstring_id: str, all_test_ids: list[str], repo_path: str, python: str
) -> tuple[list[str], list[str]]:
    """Handle test IDs that are docstrings (verbose output) rather than method names.

    Strategy: run the entire aggregation/expressions/queries suite with -v2 and
    look for the docstring followed by '... ok' in the output.
    """
    tests_dir = os.path.join(repo_path, "tests")
    runtests = os.path.join(tests_dir, "runtests.py")
    if not os.path.exists(runtests):
        return [], [docstring_id]

    # Run broad modules likely to contain this test
    for module in ["aggregation", "expressions", "queries"]:
        result = subprocess.run(
            [python, "runtests.py", "--verbosity=2", "--parallel=1", module],
            cwd=tests_dir, capture_output=True, text=True, timeout=300,
        )
        output = result.stdout + result.stderr
        needle = docstring_id[:40]  # first 40 chars to match docstring prefix
        if needle.lower() in output.lower():
            # Check if the docstring line ends with '... ok' (pass) or 'FAIL'/'ERROR'
            for line in output.splitlines():
                if needle.lower() in line.lower():
                    if "ok" in line.lower():
                        return [docstring_id], []
                    elif "fail" in line.lower() or "error" in line.lower():
                        return [], [docstring_id]
            # Found the module but unclear status — check overall result
            if "OK" in output and "FAILED" not in output:
                return [docstring_id], []

    return [], [docstring_id]


def _django_id_to_dotted(test_id: str) -> str:
    """'test_name (module.ClassName)' → 'module.ClassName.test_name'"""
    if " (" not in test_id or not test_id.endswith(")"):
        return test_id  # can't parse — return as-is for best-effort matching
    name, rest = test_id.split(" (", 1)
    module_class = rest.rstrip(")")
    return f"{module_class}.{name}"


def _django_id_to_pytest(test_id: str, repo_path: str) -> str:
    """'test_name (module.ClassName)' → 'tests/module/path.py::ClassName::test_name'"""
    name, rest = test_id.split(" (", 1)
    module_class = rest.rstrip(")")
    parts = module_class.rsplit(".", 1)
    if len(parts) == 2:
        module_path, class_name = parts
        file_path = os.path.join("tests", module_path.replace(".", os.sep) + ".py")
        return f"{file_path}::{class_name}::{name}"
    return test_id


def _parse_pytest_output(output: str, test_ids: list[str]) -> tuple[list[str], list[str]]:
    """Parse pytest -v output into (passed, failed) lists.

    Handles both per-test lines from -v mode:
      path::test_name PASSED   [33%]
      path::test_name FAILED   [33%]
    And the short test summary (present in both -q and -v):
      FAILED path::test_name - reason
    Tests not mentioned in either are marked failed (e.g. collection errors).
    """
    passed: list[str] = []
    failed: list[str] = []

    for line in output.splitlines():
        line = line.strip()
        if " PASSED" in line:
            for tid in test_ids:
                if tid.split("::")[-1] in line and tid not in passed:
                    passed.append(tid)
        elif " FAILED" in line or line.startswith("FAILED ") or " ERROR" in line:
            for tid in test_ids:
                if tid.split("::")[-1] in line and tid not in failed:
                    failed.append(tid)

    seen = set(passed) | set(failed)
    for tid in test_ids:
        if tid not in seen:
            failed.append(tid)

    return passed, failed
