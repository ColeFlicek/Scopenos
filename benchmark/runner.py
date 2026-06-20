"""
Prompt builders and patch capture for the Phronosis SWE-bench benchmark.

The Claude Code orchestrator (not this script) spawns the actual subagents
via the Agent tool. This module provides:
  - build_prompt_a / build_prompt_b  — subagent instructions per path
  - capture_patch                    — git diff after the subagent finishes
  - AgentResult                      — result dataclass written by the orchestrator
"""
from __future__ import annotations

import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from .loader import BenchmarkTask
from .repo_setup import RepoContext


@dataclass
class AgentResult:
    instance_id: str
    path: str                 # "a" or "b"
    patch: str                # unified diff of changes made
    tool_calls: list[dict]    # tool names used (recorded by orchestrator from agent output)
    iterations: int
    submitted: bool
    notes: str = ""           # agent's self-reported summary
    error: str | None = None


def build_prompt_a(task: BenchmarkTask, ctx: RepoContext) -> str:
    """System + user prompt for Path A (baseline — standard tools only)."""
    return textwrap.dedent(f"""\
        You are an expert software engineer fixing a real bug in pytest.

        IMPORTANT: Use only standard code investigation tools — grep, file reads,
        bash commands. Do NOT use any Phronosis or code-intelligence MCP tools.

        The repository is checked out at: {ctx.repo_path}
        Python venv for running tests: {ctx.venv_python}

        ## Bug to fix

        {task.problem_statement}

        ## Failing tests (must pass after your fix)

        {chr(10).join(task.fail_to_pass)}

        ## Instructions

        1. Explore the codebase to understand the bug (grep, read files, bash).
        2. Identify the root cause.
        3. Apply a minimal fix — only change what is necessary.
        4. Verify with: `{ctx.venv_python} -m pytest {' '.join(task.fail_to_pass[:3])}`
        5. When all failing tests pass, stop. Do not call any submit tool —
           the orchestrator will capture your diff automatically.

        Make minimal changes. Do not refactor unrelated code.
    """)


def build_prompt_b(task: BenchmarkTask, ctx: RepoContext) -> str:
    """System + user prompt for Path B (Phronosis-assisted)."""
    return textwrap.dedent(f"""\
        You are an expert software engineer fixing a real bug in pytest.
        You have access to Phronosis code intelligence tools (MCP) — use them.

        The repository is checked out at: {ctx.repo_path}
        Phronosis project_id for this checkout: {ctx.project_id}
        Python venv for running tests: {ctx.venv_python}

        ## Bug to fix

        {task.problem_statement}

        ## Failing tests (must pass after your fix)

        {chr(10).join(task.fail_to_pass)}

        ## Instructions

        1. Call `get_project_home("{ctx.project_id}")` for an architectural overview.
        2. Use `query_similar_functions` to find the relevant code.
        3. Use `get_callers` / `get_callees` / `get_impact_radius` before editing.
        4. Apply a minimal fix.
        5. Verify with: `{ctx.venv_python} -m pytest {' '.join(task.fail_to_pass[:3])}`
        6. When all failing tests pass, stop. The orchestrator will capture your diff.

        Use Phronosis to understand relationships — don't grep blindly when the
        call graph already has the answer. Make minimal changes only.
    """)


def capture_patch(repo_path: str) -> str:
    """Return unified diff of all uncommitted changes in the checkout."""
    result = subprocess.run(
        ["git", "diff", "--unified=3"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout


def save_patch(patch: str, results_dir: str, instance_id: str, path: str) -> Path:
    """Write patch.diff to results/{instance_id}/path_{a|b}/patch.diff."""
    out = Path(results_dir) / instance_id / f"path_{path}" / "patch.diff"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(patch)
    return out
