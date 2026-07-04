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

        ## Investigation protocol

        Use two modes throughout the entire investigation — not just at the start:

        **Navigation mode** (where is X? who calls Y? what else changes?):
        → Always reach for Scopenos MCP tools first. They return summaries and
          call-graph facts without reading any file.

        **Implementation mode** (what does the exact code look like?):
        → Read the specific file, only after navigation has identified it.

        The loop is: MCP → Read → if a new question about relationships arises → MCP again.
        Do not switch to file-reading mode and stay there. Every time you hit
        a "where does this come from?" or "who else uses this?" question, go
        back to MCP before opening another file.

        BASH DISCIPLINE: Use Bash ONLY for the single final pytest verify call.
        No grep, no cat, no intermediate test runs.

        ---

        ## When to use MCP vs Read

        ### MCP is better than Read when:

        **Finding which file/function is relevant**
        - Bad: open `db/query.py` hoping the bug is there, then `db/lookups.py`, then `db/expressions.py`
        - Good: `query_similar_functions("exclude subquery deduplication")` → returns the one function with a summary

        **Understanding who calls or uses something**
        - Bad: grep for `split_exclude` across 500 files, read each match for context
        - Good: `get_callers("django.db.models.sql.query.Query.split_exclude")` → full caller list with signatures instantly

        **Checking what else needs to change**
        - Bad: make the fix, run tests, discover a sibling method also needed updating
        - Good: `get_impact_radius(fn)` → `co_change_hints` lists protocol gaps before you start editing

        **Understanding a test subsystem before opening test files**
        - Bad: open `tests/queries/tests.py` (3000 lines), scan for relevant class
        - Good: `get_subsystem_detail(project_id, "tests.queries")` → lists test classes and summaries, then open only the one you need

        **A new navigation question appears mid-investigation**
        - Bad: you're reading `expressions.py` and wonder "where is Exists constructed?" → open `__init__.py`, then `query.py`
        - Good: stop, call `get_callers("Exists.__init__")` → answer in one call, then open only the file it points to

        ### Read is better than MCP when:

        **You need the exact implementation to write a correct edit**
        - MCP summaries describe what a function does; they don't show the line you need to change.
          Once MCP has told you the file and function, Read that specific function.

        **The function isn't indexed or MCP returns empty**
        - Fall back to Read (or grep) when `query_similar_functions` returns no results for a concept.
          This is a signal the index may be stale — note it in your tool log.

        **Verifying exact argument names, return types, or local variable names**
        - MCP knows signatures but not every local variable. For exact syntax, Read the function body.

        ---

        ### Start — orient once
        Call `mcp__scopenos__get_project_home("{ctx.project_id}")`.
        Identify which subsystem contains the relevant code.
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

        ### Navigate — find the function (navigation mode)
        Call `mcp__scopenos__query_similar_functions("<key concept from bug>", project_id="{ctx.project_id}")`.
        Use the top result's `id` and `file` for the next navigation calls.
        Example output (truncated):
        {{
          "results": [
            {{"id": "pkg.core.Engine.process",
              "summary": "Processes incoming request and dispatches to handler",
              "file": "pkg/core/engine.py",
              "signature": "def process(self, request, **kwargs)"}},
            ...
          ]
        }}

        ### Navigate — check impact and co-changes (navigation mode)
        Call `mcp__scopenos__get_impact_radius("<id>", project_id="{ctx.project_id}")`.
        `co_change_hints` surfaces protocol gaps and semantic siblings that call-graph
        traversal alone won't find — read these before deciding what to fix.
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

        ### Navigate — call-graph questions (navigation mode, use as needed)
        Before opening any file to answer a "who calls this?" or "what does this call?" question:
        - `mcp__scopenos__get_callers("<id>", project_id="{ctx.project_id}")` — all callers
          Example: {{"callers": [{{"id": "pkg.server.Server.handle", "file": "pkg/server.py",
            "signature": "def handle(self, conn)"}}], ...}}
        - `mcp__scopenos__get_callees("<id>", project_id="{ctx.project_id}")` — all callees
          Example: {{"callees": [{{"id": "pkg.io.Reader.read", "is_external": false}},
            {{"id": "external.socket.recv", "is_external": true}}], ...}}
        - `mcp__scopenos__get_subsystem_detail("{ctx.project_id}", "<subsystem>")` — fixtures,
          helpers, and patterns within a subsystem; use before reading test files.
          Example: {{"subsystem": "tests.core",
            "anchor_summary": "Integration tests for Engine dispatch and routing",
            "top_functions": [{{"id": "tests.core.EngineTests.test_dispatch",
              "summary": "Tests dispatch with valid and invalid payloads"}}, ...],
            "connections": [{{"from": "tests.core", "to": "pkg.core", "edge_count": 29}}]}}

        ### Read — implementation details (implementation mode)
        Only open a file when you need the exact code to write an edit.
        If reading triggers a new navigation question (e.g. "where does this
        argument come from?"), stop and call an MCP tool before reading further.

        ### Fix — apply a minimal fix
        Edit only what the bug requires. No unrelated changes.

        ### Verify
        `{ctx.venv_python} -m pytest {' '.join(pytest_ids)}`
        All failing tests must pass. When they do, stop.

        ### Output tool log
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
