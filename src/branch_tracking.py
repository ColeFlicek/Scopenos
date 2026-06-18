"""Branch-aware coordination for shared project indexes.

Owns all branch semantics: git detection, canonical trunk-branch names, and
conflict classification. Storage queries stay in CallGraphDB; this module adds
the domain logic on top so it can be tested without a database connection.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass

# Canonical trunk branch names. Extend here to treat "develop" or "release/*"
# as main-branch equivalents — the change propagates to all conflict detection.
MAIN_BRANCHES: frozenset[str] = frozenset({"main", "master"})


@dataclass(frozen=True)
class BranchContext:
    """Current git branch and HEAD commit for a project path."""
    branch: str
    head_commit: str

    @property
    def is_main(self) -> bool:
        return self.branch in MAIN_BRANCHES


def detect_branch(path: str) -> BranchContext:
    """Return the current branch and HEAD commit for the git repo at path.

    Returns BranchContext("", "") if path is not a git repo or git is unavailable.
    """
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=path, timeout=5,
        ).stdout.strip()
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=path, timeout=5,
        ).stdout.strip()
        return BranchContext(branch=branch or "", head_commit=head or "")
    except Exception:
        return BranchContext(branch="", head_commit="")


def classify_conflicts(rows: list[dict]) -> dict:
    """Pure function: group and classify raw branch_function_changes rows.

    rows: list of {branch, function_id, head_commit, modified_at} dicts,
          already filtered to exclude the caller's own branch.

    Returns the standard conflict response shape used by get_branch_conflicts
    and the get_branch_conflicts MCP tool.
    """
    conflicts_by_fn: dict[str, list[dict]] = {}
    for row in rows:
        fn_id = row["function_id"]
        conflicts_by_fn.setdefault(fn_id, []).append({
            "branch": row["branch"],
            "head_commit": row["head_commit"],
            "modified_at": row["modified_at"],
        })

    conflicts = []
    main_drift: list[str] = []
    branches_seen: set[str] = set()
    for fn_id, touches in conflicts_by_fn.items():
        branches = [t["branch"] for t in touches]
        branches_seen.update(branches)
        is_drifted = any(b in MAIN_BRANCHES for b in branches)
        if is_drifted:
            main_drift.append(fn_id)
        conflicts.append({
            "function_id": fn_id,
            "competing_branches": touches,
            "main_drift": is_drifted,
        })

    return {
        "conflicts": conflicts,
        "main_drift": main_drift,
        "summary": {
            "total": len(conflicts),
            "branches": sorted(branches_seen),
            "functions_with_main_drift": len(main_drift),
        },
    }


def empty_conflict_result() -> dict:
    """Canonical empty response when there are no function IDs to query."""
    return {
        "conflicts": [],
        "main_drift": [],
        "summary": {"total": 0, "branches": [], "functions_with_main_drift": 0},
    }
