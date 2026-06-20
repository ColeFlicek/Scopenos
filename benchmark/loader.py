"""
Load and filter SWE-bench Lite tasks.

Fetches from HuggingFace on first call; cached locally after that.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass
class BenchmarkTask:
    """One SWE-bench task — a real bug with a verifiable fix."""
    instance_id: str       # e.g. "pytest-dev__pytest-11143"
    repo: str              # e.g. "pytest-dev/pytest"
    base_commit: str       # the commit with the bug (agents start here)
    problem_statement: str # what the agent sees
    fail_to_pass: list[str]  # tests that must pass after fix (evaluation criterion)
    pass_to_pass: list[str]  # tests that must still pass (regression guard)
    # ground_truth_patch is intentionally excluded — agents must not see it


def load_tasks(
    repo: str = "pytest-dev/pytest",
    cache_dir: str | None = None,
) -> list[BenchmarkTask]:
    """Load SWE-bench Lite tasks for a repo, deduplicated by base_commit."""
    from datasets import load_dataset

    ds = load_dataset(
        "princeton-nlp/SWE-bench_Lite",
        split="test",
        cache_dir=cache_dir,
    )

    tasks = []
    seen_commits: set[str] = set()

    for row in ds:
        if row["repo"] != repo:
            continue
        commit = row["base_commit"]
        if commit in seen_commits:
            continue
        seen_commits.add(commit)

        fail = _parse_list(row["FAIL_TO_PASS"])
        if not fail:
            continue  # skip tasks with no verifiable test

        tasks.append(BenchmarkTask(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=commit,
            problem_statement=row["problem_statement"],
            fail_to_pass=fail,
            pass_to_pass=_parse_list(row["PASS_TO_PASS"]),
        ))

    return sorted(tasks, key=lambda t: t.instance_id)  # stable for dedup; caller re-sorts by date


def load_tasks_chronological(
    repo: str = "pytest-dev/pytest",
    cache_dir: str | None = None,
) -> list[BenchmarkTask]:
    """Load tasks sorted oldest→newest by created_at (for commit-stepping with index_changes)."""
    from datasets import load_dataset

    ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test", cache_dir=cache_dir)

    tasks = []
    seen_commits: set[str] = set()

    for row in sorted(ds, key=lambda r: r.get("created_at", "")):
        if row["repo"] != repo:
            continue
        commit = row["base_commit"]
        if commit in seen_commits:
            continue
        seen_commits.add(commit)

        fail = _parse_list(row["FAIL_TO_PASS"])
        if not fail:
            continue

        tasks.append(BenchmarkTask(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=commit,
            problem_statement=row["problem_statement"],
            fail_to_pass=fail,
            pass_to_pass=_parse_list(row["PASS_TO_PASS"]),
        ))

    return tasks


def select_calibration_tasks(tasks: list[BenchmarkTask], n_hard: int = 3, n_random: int = 2) -> list[BenchmarkTask]:
    """
    Pick n_hard tasks with the most FAIL_TO_PASS tests (proxy for complexity)
    plus n_random from the remainder.
    """
    import random
    sorted_by_complexity = sorted(tasks, key=lambda t: len(t.fail_to_pass), reverse=True)
    hard = sorted_by_complexity[:n_hard]
    rest = [t for t in tasks if t not in hard]
    random.seed(42)
    easy = random.sample(rest, min(n_random, len(rest)))
    return hard + easy


INDEXED_REPOS = {
    "django/django": "django",
    "scikit-learn/scikit-learn": "scikit-learn",
    "matplotlib/matplotlib": "matplotlib",
    "pydata/xarray": "xarray",
    "sphinx-doc/sphinx": "sphinx",
    "mwaskom/seaborn": "seaborn",
    "pylint-dev/pylint": "pylint",
    "pytest-dev/pytest": "pytest",
}


def load_multifile_tasks(
    repos: list[str] | None = None,
    min_files: int = 2,
    max_tasks: int = 20,
    cache_dir: str | None = None,
) -> list[BenchmarkTask]:
    """
    Load SWE-bench Full tasks where the ground-truth patch touches ≥ min_files files.
    Restricts to repos already indexed in Phronosis (best Phronosis advantage).

    These are the tasks most likely to reveal Path B > Path A:
    - Multi-file patches require understanding cross-file relationships
    - Phronosis call graph / impact_radius surfaces root causes across files
    - Grep-based Path A tends to patch the symptom file, not the root cause
    """
    from datasets import load_dataset

    target_repos = set(repos) if repos else set(INDEXED_REPOS.keys())

    try:
        ds = load_dataset("princeton-nlp/SWE-bench", split="test", cache_dir=cache_dir)
    except Exception:
        ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test", cache_dir=cache_dir)

    tasks = []
    seen: set[str] = set()

    for row in ds:
        if row["repo"] not in target_repos:
            continue
        if row["instance_id"] in seen:
            continue

        fail = _parse_list(row["FAIL_TO_PASS"])
        if not fail:
            continue

        patch = row.get("patch", "")
        files_changed = _count_patch_files(patch)
        if files_changed < min_files:
            continue

        seen.add(row["instance_id"])
        tasks.append(BenchmarkTask(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row["problem_statement"],
            fail_to_pass=fail,
            pass_to_pass=_parse_list(row["PASS_TO_PASS"]),
        ))

        if len(tasks) >= max_tasks:
            break

    return tasks


def _count_patch_files(patch: str) -> int:
    """Count how many files a unified diff touches."""
    return sum(1 for line in patch.splitlines() if line.startswith("diff --git "))


def _parse_list(value) -> list[str]:
    """Normalise FAIL_TO_PASS / PASS_TO_PASS — may be JSON string or list."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return [value] if value.strip() else []
    return list(value) if value else []
