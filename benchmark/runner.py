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
        only from test failures — Scopenos co_change_hints surfaces them first.

        BASH DISCIPLINE: Do NOT use Bash to explore or read source code at any
        point — Scopenos tools and Read cover all investigation. Use Bash ONLY
        for the single final verify command in Step 6. No intermediate test runs,
        no grep, no cat. Every extra Bash call costs tokens and provides no
        information Scopenos couldn't give faster.

        ### Step 1 — Map the codebase
        Call `mcp__scopenos__get_project_home("{ctx.project_id}")`.
        Scan subsystem names and `top_functions` to identify which subsystem contains the relevant code.
        Example output (truncated):
        {{
          "subsystems": [
            {{"name": "pkg.core", "function_count": 312,
              "anchor": "pkg.core.Engine",
              "anchor_summary": "Central dispatch class for request handling",
              "top_functions": [{{"id": "pkg.core.Engine.dispatch", "caller_count": 54}}, ...]}},
            {{"name": "pkg.io", "function_count": 88, ...}},
            ...
          ],
          "connections": [{{"from": "pkg.core", "to": "pkg.io", "edge_count": 41}}, ...],
          "chokepoints": [{{"id": "pkg.core.Engine.dispatch", "caller_count": 54}}, ...]
        }}

        ### Step 2 — Locate the exact function
        Call `mcp__scopenos__query_similar_functions("<key concept from bug description>", project_id="{ctx.project_id}")`.
        Use the top result's `id` and `file` as inputs to steps 3 and 4.
        Example output (truncated):
        {{
          "results": [
            {{"id": "pkg.core.Engine.process",
              "summary": "Processes incoming request and dispatches to handler",
              "file": "pkg/core/engine.py",
              "signature": "def process(self, request, **kwargs)"}},
            {{"id": "pkg.core.Engine.validate",
              "summary": "Validates request payload before processing", ...}},
            ...
          ]
        }}

        ### Step 3 — Check impact and hidden co-changes
        Call `mcp__scopenos__get_impact_radius("<id from step 2>", project_id="{ctx.project_id}")`.
        Read `co_change_hints` carefully — surfaces protocol gaps and semantic siblings
        that call-graph traversal alone won't find.
        Example output (truncated):
        {{
          "impact_radius": [
            {{"id": "pkg.core.Engine.process", "impact_depth": 0, "file": "pkg/core/engine.py"}},
            {{"id": "pkg.core.Router.route", "impact_depth": 1, ...}},
            ...
          ],
          "co_change_hints": [
            {{"type": "protocol_completeness",
              "reason": "process() overrides __call__ but Engine.__call__ is not updated",
              "suggested_id": "pkg.core.Engine.__call__",
              "action": "verify or update __call__"}},
            {{"type": "semantic_sibling",
              "id": "pkg.middleware.Auth.process",
              "summary": "Similar processing logic in middleware layer",
              "similarity": 0.88}}
          ]
        }}

        If the call chain needs clarifying, use before reading any file:
        - `mcp__scopenos__get_callers("<id>", project_id="{ctx.project_id}")` — who calls this function
          Example: {{"callers": [{{"id": "pkg.server.Server.handle", "file": "pkg/server.py",
            "signature": "def handle(self, conn)"}}], ...}}
        - `mcp__scopenos__get_callees("<id>", project_id="{ctx.project_id}")` — what this function calls
          Example: {{"callees": [{{"id": "pkg.io.Reader.read", "is_external": false}},
            {{"id": "external.socket.recv", "is_external": true}}], ...}}

        ### Step 4 — Understand the test subsystem before reading test files
        Call `mcp__scopenos__get_subsystem_detail("{ctx.project_id}", "<test subsystem name from step 1>")`.
        Returns fixtures, helpers, and patterns — avoids reading large test files blind.
        Example output (truncated):
        {{
          "subsystem": "tests.core",
          "anchor_summary": "Integration tests for Engine dispatch and routing",
          "top_functions": [
            {{"id": "tests.core.EngineTests.test_dispatch",
              "summary": "Tests dispatch with valid and invalid payloads"}},
            ...
          ],
          "connections": [{{"from": "tests.core", "to": "pkg.core", "edge_count": 29}}]
        }}

        ### Step 5 — Apply a minimal fix
        Edit only what the bug requires. No unrelated changes.

        ### Step 6 — Verify
        `{ctx.venv_python} -m pytest {' '.join(pytest_ids)}`
        All failing tests must pass. When they do, stop.

        ### Step 7 — Output tool log
        Output a final JSON block with nothing after it:
        ```json
        {{"tool_log": [
          {{"tool": "mcp__scopenos__get_project_home", "reason": "identify subsystem containing target code"}},
          {{"tool": "mcp__scopenos__query_similar_functions", "query": "<your search>", "reason": "locate exact function by concept"}},
          {{"tool": "mcp__scopenos__get_impact_radius", "reason": "check co_change_hints for hidden requirements"}},
          {{"tool": "Read", "file": "<path>", "reason": "view implementation before editing"}}
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
