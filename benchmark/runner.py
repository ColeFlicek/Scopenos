"""
Prompt builders and patch capture for the Scopenos SWE-bench benchmark.

The Claude Code orchestrator (not this script) spawns the actual subagents
via the Agent tool. This module provides:
  - build_prompt_a / build_prompt_b  — subagent instructions per path
  - capture_patch                    — git diff after the subagent finishes
  - parse_tool_log                   — extract tool_log JSON from agent output text
  - AgentResult                      — result dataclass written by the orchestrator
"""
from __future__ import annotations

import json
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from .loader import BenchmarkTask
from .repo_setup import RepoContext


def _to_pytest_ids(test_ids: list[str], repo_path: str) -> list[str]:
    """
    Convert SWE-bench test IDs to pytest-runnable form.

    Django (and some other repos) emit unittest-style IDs:
        "test_equality (lookup.test_lookups.LookupTests)"
    pytest requires:
        "tests/lookup/test_lookups.py::LookupTests::test_equality"

    If the ID doesn't match the pattern it is returned unchanged.
    """
    out = []
    for tid in test_ids:
        m = re.match(r"^(\w+) \(([^)]+)\)$", tid)
        if not m:
            out.append(tid)
            continue
        test_name, dotted = m.group(1), m.group(2)
        parts = dotted.rsplit(".", 1)
        if len(parts) == 2:
            module_path, class_name = parts
            file_rel = "tests/" + module_path.replace(".", "/") + ".py"
            out.append(f"{file_rel}::{class_name}::{test_name}")
        else:
            out.append(tid)
    return out


@dataclass
class AgentResult:
    instance_id: str
    path: str                      # "a" or "b"
    patch: str                     # unified diff of changes made
    tool_calls: list[dict]         # [{tool, reason, ...}] parsed from agent JSON output
    iterations: int
    submitted: bool
    notes: str = ""                # agent's self-reported one-sentence summary
    error: str | None = None
    agent_tokens: int = 0          # subagent_tokens from Agent tool completion notification


def parse_tool_log(agent_output: str) -> tuple[list[dict], str]:
    """
    Extract tool_log and notes from the JSON block the agent emits at the end.

    Returns (tool_log, notes). On parse failure returns ([], "").

    Expected agent output format (last ```json block in the response):
        ```json
        {
          "tool_log": [
            {"tool": "get_project_home", "reason": "orient to codebase"},
            {"tool": "Read", "file": "src/foo.py", "reason": "view implementation"}
          ],
          "notes": "Found bug in X, fixed by Y."
        }
        ```
    """
    # Find the last ```json ... ``` block
    blocks = re.findall(r"```json\s*(.*?)\s*```", agent_output, re.DOTALL)
    if not blocks:
        return [], ""
    try:
        data = json.loads(blocks[-1])
    except (json.JSONDecodeError, ValueError):
        return [], ""

    raw = data.get("tool_log", [])
    notes = data.get("notes", "")

    # Normalise: accept both string entries ["Bash", "Read"] and dict entries
    tool_log: list[dict] = []
    for entry in raw:
        if isinstance(entry, str):
            tool_log.append({"tool": entry, "reason": ""})
        elif isinstance(entry, dict):
            tool_log.append(entry)

    return tool_log, notes


def build_prompt_a(task: BenchmarkTask, ctx: RepoContext) -> str:
    """System + user prompt for Path A (baseline — standard tools only)."""
    pytest_ids = _to_pytest_ids(task.fail_to_pass, ctx.repo_path)
    return textwrap.dedent(f"""\
        You are an expert software engineer fixing a real bug in {task.repo}.

        IMPORTANT: Use only standard code investigation tools — grep, file reads,
        bash commands. Do NOT use any Scopenos or code-intelligence MCP tools.

        The repository is checked out at: {ctx.repo_path}
        Python venv for running tests: {ctx.venv_python}

        ## Bug to fix

        {task.problem_statement}

        ## Failing tests (must pass after your fix)

        {chr(10).join(pytest_ids)}

        ## Instructions

        1. Explore the codebase to understand the bug (grep, read files, bash).
        2. Identify the root cause.
        3. Apply a minimal fix — only change what is necessary.
        4. Verify with: `{ctx.venv_python} -m pytest {' '.join(pytest_ids[:3])}`
        5. When all failing tests pass, stop. Do not call any submit tool —
           the orchestrator will capture your diff automatically.

        Make minimal changes. Do not refactor unrelated code.

        6. Output a final JSON block (nothing after it):
           ```json
           {{"tool_log": [
             {{"tool": "Bash", "reason": "grep for skip handling in python.py"}},
             {{"tool": "Read", "file": "src/_pytest/python.py", "reason": "view collect_one_item before editing"}}
           ], "notes": "one sentence — what the bug was and how you fixed it"}}
           ```
           List every tool call in order. `reason` is 5-10 words explaining why.
    """)


def build_prompt_b(task: BenchmarkTask, ctx: RepoContext) -> str:
    """System + user prompt for Path B (Scopenos-assisted)."""
    pytest_ids = _to_pytest_ids(task.fail_to_pass, ctx.repo_path)
    return textwrap.dedent(f"""\
        You are an expert software engineer fixing a real bug in {task.repo}.
        You have access to Scopenos code intelligence tools (MCP) — use them.

        IMPORTANT: Use `mcp__scopenos__*` tools (the primary Scopenos server).
        Do NOT use `mcp__scopenos_bench__*` tools.

        BENCHMARK INTEGRITY: Do NOT read any files under `benchmark/` — previous
        run patches and reference solutions live there and would contaminate results.

        The repository is checked out at: {ctx.repo_path}
        Scopenos project_id for this checkout: {ctx.project_id}
        Python venv for running tests: {ctx.venv_python}

        ## Bug to fix

        {task.problem_statement}

        ## Failing tests (must pass after your fix)

        {chr(10).join(pytest_ids)}

        ## Protocol — complete in order

        DO NOT call Read, grep, or Bash on source files until steps 1–3 are
        complete. Skipping these gates means discovering hidden requirements
        (e.g. missing __hash__ when adding __eq__) only from test failures.

        BASH DISCIPLINE: Do NOT use Bash to explore or read source code at any
        point — Scopenos tools and Read cover all investigation. Use Bash ONLY
        for the single final verify command in Step 6. No intermediate test runs,
        no grep, no cat. Every extra Bash call costs tokens and provides no
        information Scopenos couldn't give faster.

        ### Step 1 — Map the codebase
        Call `mcp__scopenos__get_project_home("{ctx.project_id}")`.
        Scan `top_functions` in each subsystem to locate the relevant code — no grep needed.
        Example output (truncated):
        {{
          "subsystems": [
            {{"name": "django.db.models", "function_count": 412,
              "anchor": "django.db.models.Model",
              "anchor_summary": "Base class for all ORM model instances",
              "top_functions": [{{"id": "django.db.models.Model.__eq__", "caller_count": 38}}, ...]}},
            ...
          ],
          "connections": [{{"from": "django.db.models", "to": "django.db.models.sql", "edge_count": 84}}, ...],
          "chokepoints": [{{"id": "django.db.models.Model.save", "caller_count": 201}}, ...]
        }}

        ### Step 2 — Locate the exact function
        Call `mcp__scopenos__query_similar_functions("<concept from bug description>", project_id="{ctx.project_id}")`.
        Use the returned `id` values as inputs to steps 3 and 4.
        Example output (truncated):
        {{
          "results": [
            {{"id": "django.db.models.Model.__eq__",
              "summary": "Compare model instances by pk",
              "file": "django/db/models/base.py",
              "signature": "def __eq__(self, other)"}},
            {{"id": "django.db.models.Model.__hash__",
              "summary": "Hash model instance by pk", ...}},
            ...
          ]
        }}

        ### Step 3 — Check impact and hidden co-changes
        Call `mcp__scopenos__get_impact_radius("<id from step 2>", project_id="{ctx.project_id}")`.
        Read `co_change_hints` carefully — it surfaces protocol gaps and semantic
        siblings NOT reachable via call edges.
        Example output (truncated):
        {{
          "impact_radius": [
            {{"id": "django.db.models.Model.__eq__", "impact_depth": 0, "file": "django/db/models/base.py"}},
            {{"id": "django.db.models.Model.pk", "impact_depth": 1, ...}},
            ...
          ],
          "co_change_hints": [
            {{"type": "protocol_completeness",
              "reason": "__eq__ defined but __hash__ is missing on django.db.models.Model",
              "suggested_id": "django.db.models.Model.__hash__",
              "action": "add or verify __hash__"}},
            {{"type": "semantic_sibling",
              "id": "django.contrib.auth.models.AbstractUser.__eq__",
              "summary": "Similar equality logic — may need same fix",
              "similarity": 0.91}}
          ]
        }}

        If the call chain needs clarifying, use these tools before reading any file:
        - `mcp__scopenos__get_callers("<id>", project_id="{ctx.project_id}")` — every function that calls this one
          Example: {{"callers": [{{"id": "django.test.TestCase.assertQuerysetEqual",
            "file": "django/test/testcases.py",
            "signature": "def assertQuerysetEqual(self, qs, values, ...)"}}], ...}}
        - `mcp__scopenos__get_callees("<id>", project_id="{ctx.project_id}")` — every function this one calls
          Example: {{"callees": [{{"id": "django.db.models.sql.compiler.SQLCompiler.execute_sql",
            "is_external": false}},
            {{"id": "external.builtins.hash", "is_external": true}}], ...}}

        ### Step 4 — Understand the test subsystem before reading test files
        Call `mcp__scopenos__get_subsystem_detail("{ctx.project_id}", "<test subsystem name from step 1>")`.
        This returns existing fixtures, helpers, and test patterns — avoids reading large test files blind.
        Example output (truncated):
        {{
          "subsystem": "tests.model_tests",
          "anchor_summary": "Base model test fixtures and assertion helpers",
          "top_functions": [
            {{"id": "tests.model_tests.ModelTests.test_eq",
              "summary": "Tests Model.__eq__ with pk comparison"}},
            ...
          ],
          "connections": [{{"from": "tests.model_tests", "to": "django.db.models", "edge_count": 47}}]
        }}

        ### Step 5 — Apply a minimal fix
        Edit only what the bug requires. No unrelated changes.

        ### Step 6 — Verify
        `{ctx.venv_python} -m pytest {' '.join(pytest_ids[:3])}`
        All failing tests must pass. When they do, stop.

        ### Step 7 — Output tool log
        Output a final JSON block with nothing after it:
        ```json
        {{"tool_log": [
          {{"tool": "mcp__scopenos__get_project_home", "reason": "map codebase, find django.db subsystem"}},
          {{"tool": "mcp__scopenos__query_similar_functions", "query": "Model __eq__ comparison", "reason": "locate exact function"}},
          {{"tool": "mcp__scopenos__get_impact_radius", "reason": "check co_change_hints for protocol gaps"}},
          {{"tool": "Read", "file": "django/db/models/base.py", "reason": "view __eq__ before editing"}}
        ], "notes": "one sentence — what the bug was and how you fixed it"}}
        ```
        Every tool call in order. `reason` is 5-10 words. For Scopenos tools include
        `query` if you passed a search string. For Read/Edit include `file`.
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
