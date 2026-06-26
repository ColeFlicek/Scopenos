"""
Load and filter SWE-bench Lite tasks.

Fetches from HuggingFace on first call; cached locally after that.

Fork workflow integration
--------------------------
Each BenchmarkTask carries a ``base_commit`` — the commit at which the bug is
present and agents must start work.  The intended harness flow using Scopenos
forks is:

1. **Index the repo once** at current HEAD::

       index_project("/path/to/repo", project_id="pytest")

2. **For each task**, create a fork at ``base_commit`` so Scopenos sees the
   call graph as it existed when the bug was present::

       result = await fork_project(project_id="pytest",
                                   commit_ref=task.base_commit)
       fork_id = result["fork_project_id"]  # e.g. "pytest_fork_abc1234"

3. **Run the agent** against ``fork_id`` — all Scopenos queries (get_callers,
   get_impact_radius, query_similar_functions) reflect the buggy state.

4. **Clean up** after the agent run to reclaim schema space::

       await drop_fork(fork_id)

This lets you evaluate Scopenos across every task commit without re-indexing
the full repo for each task.  The benchmark runner (not this loader) is
responsible for driving these three calls per task.
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


# Tasks where grep-based Path A is structurally likely to fail.
# All tasks load from princeton-nlp/SWE-bench (full), not Lite —
# full has multi-file patches that require cross-file reasoning.
#
# Three categories:
#   1. protocol_pair   — fix requires adding a method pair (__eq__+__hash__, etc.)
#                        Scopenos fires `protocol_completeness` co_change_hint.
#   2. visitor_pattern — method dispatched by name (_print_X, _visit_X); doesn't
#                        exist yet so grep finds nothing. Multi-file entries require
#                        the same handler added to N concrete visitor classes at once.
#   3. sibling_class   — same bug in 2+ sibling classes; problem statement mentions
#                        one. Scopenos `semantic_sibling` hint surfaces the others.
#
# files= column shows ground-truth patch size; multi-file tasks need cross-file nav.
_PATH_A_HARD_TASKS: dict[str, list[str]] = {
    "protocol_pair": [
        "django__django-13220",   # files=1  ValidationError needs __eq__ + __hash__
        "django__django-13606",   # files=4  Lookup needs __eq__ + __hash__ (+ NOT EXISTS)
        "django__django-14672",   # files=1  ManyToManyRel missing make_hashable / __hash__
        "django__django-14915",   # files=1  ModelChoiceIteratorValue needs __hash__
        "django__django-11964",   # files=1  TextChoices/IntegerChoices __str__ broken
    ],
    "visitor_pattern": [
        # Single-file: handler missing from one concrete visitor
        "sympy__sympy-11400",     # files=1  CCodePrinter missing _print_Relational, _print_sinc
        "sympy__sympy-12171",     # files=1  printer missing _print_Derivative
        "sympy__sympy-15308",     # files=1  LaTeXPrinter missing _print_Basic, _print_Trace
        "sympy__sympy-16106",     # files=1  MathMLPrinter missing _print_tuple, _print_Indexed*
        "sympy__sympy-17022",     # files=1  NumPyPrinter missing _print_Identity
        "sympy__sympy-20639",     # files=1  printer missing _print_nth_root
        "sympy__sympy-21171",     # files=1  LaTeXPrinter missing _print_SingularityFunction
        "pytest-dev__pytest-5103",# files=1  assertion rewriter missing _visit_all
        # Multi-file: same handler missing across N concrete visitor classes
        "sympy__sympy-13903",     # files=2  fcode + octave both missing same _print_ handler
        "sympy__sympy-14207",     # files=4  codeprinter + julia + octave + str all missing
        "sympy__sympy-16906",     # files=5  latex + mathml + pretty + str all missing
    ],
    "sibling_class": [
        # Single-file: bug reported in one class, sibling class has same bug
        "django__django-16041",          # files=1  FormsFormset + Jinja2FormsFormset same bug
        "django__django-16379",          # files=1  FileBasedCache + FileBasedCachePathLib same bug
        "scikit-learn__scikit-learn-14983",  # files=1  RepeatedKFold + RepeatedStratifiedKFold need __repr__
        # Multi-file: same bug across multiple backend/sibling implementations
        "django__django-11138",          # files=4  MySQL + Oracle + SQLite backends same timezone bug
        "django__django-11527",          # files=3  sqlflush + sqlmigrate + sqlsequencereset same fix
    ],
}


def load_path_a_hard_tasks(
    categories: list[str] | None = None,
    cache_dir: str | None = None,
) -> list[BenchmarkTask]:
    """
    Load the curated set of tasks where grep-based Path A is structurally likely
    to fail and Scopenos Path B has a clear advantage.

    Loads from SWE-bench Full (not Lite) — the full dataset includes multi-file
    patches that require cross-file reasoning, which is where Scopenos has the
    largest advantage over grep-based agents.

    categories: subset of ["protocol_pair", "visitor_pattern", "sibling_class"].
                Default: all three.

    Tasks are returned in a stable order: protocol_pair first, then
    visitor_pattern, then sibling_class.
    """
    from datasets import load_dataset

    target_ids: list[str] = []
    for cat in (categories or list(_PATH_A_HARD_TASKS)):
        target_ids.extend(_PATH_A_HARD_TASKS.get(cat, []))

    target_set = set(target_ids)

    ds = load_dataset("princeton-nlp/SWE-bench", split="test", cache_dir=cache_dir)
    by_id: dict[str, BenchmarkTask] = {}

    for row in ds:
        if row["instance_id"] not in target_set:
            continue
        fail = _parse_list(row["FAIL_TO_PASS"])
        if not fail:
            continue
        by_id[row["instance_id"]] = BenchmarkTask(
            instance_id=row["instance_id"],
            repo=row["repo"],
            base_commit=row["base_commit"],
            problem_statement=row["problem_statement"],
            fail_to_pass=fail,
            pass_to_pass=_parse_list(row["PASS_TO_PASS"]),
        )

    missing = [iid for iid in target_ids if iid not in by_id]
    if missing:
        print(f"[loader] WARNING: {len(missing)} task(s) not found in SWE-bench Full: {missing}")

    # Return in the stable category order defined above
    return [by_id[iid] for iid in target_ids if iid in by_id]


def load_multifile_tasks(
    repos: list[str] | None = None,
    min_files: int = 2,
    max_tasks: int = 20,
    cache_dir: str | None = None,
) -> list[BenchmarkTask]:
    """
    Load SWE-bench Full tasks where the ground-truth patch touches ≥ min_files files.
    Restricts to repos already indexed in Scopenos (best Scopenos advantage).

    These are the tasks most likely to reveal Path B > Path A:
    - Multi-file patches require understanding cross-file relationships
    - Scopenos call graph / impact_radius surfaces root causes across files
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
