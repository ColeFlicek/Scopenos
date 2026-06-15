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

    with tempfile.TemporaryDirectory(prefix="phronosis-eval-") as tmpdir:
        # Copy the checkout into a fresh directory for evaluation
        eval_path = os.path.join(tmpdir, "repo")
        subprocess.run(["cp", "-r", repo_path, eval_path], check=True)

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
        passed, failed = _run_tests(task.fail_to_pass, eval_path)

    return EvaluationResult(
        instance_id=task.instance_id,
        path=agent_result.path,
        patch_applied=True,
        tests_passed=passed,
        tests_failed=failed,
    )


def _run_tests(test_ids: list[str], repo_path: str) -> tuple[list[str], list[str]]:
    """Run a list of pytest node IDs and return (passed, failed)."""
    if not test_ids:
        return [], []

    result = subprocess.run(
        ["python", "-m", "pytest", "--no-header", "-q", "--tb=no", *test_ids],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    passed = []
    failed = []

    for line in result.stdout.splitlines():
        line = line.strip()
        if " PASSED" in line or line.endswith("passed"):
            for tid in test_ids:
                if _test_id_in_line(tid, line) and tid not in passed:
                    passed.append(tid)
        elif " FAILED" in line or " ERROR" in line:
            for tid in test_ids:
                if _test_id_in_line(tid, line) and tid not in failed:
                    failed.append(tid)

    # Any test not explicitly seen as passed is considered failed
    seen = set(passed) | set(failed)
    for tid in test_ids:
        if tid not in seen:
            failed.append(tid)

    return passed, failed


def _test_id_in_line(test_id: str, line: str) -> bool:
    # Match by the test function name (last :: component)
    test_name = test_id.split("::")[-1]
    return test_name in line
