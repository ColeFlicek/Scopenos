"""
Autonomous agent runner for SWE-bench tasks.

Path A: Claude with Read + Bash + Write tools only (realistic baseline).
Path B: Claude with Read + Bash + Write + Phronosis query tools.

The agent loop runs until Claude produces a patch or hits max_iterations.
The patch is captured as the diff between the original and modified files.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic

from .loader import BenchmarkTask
from .repo_setup import RepoContext

MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 30

SYSTEM_PROMPT = textwrap.dedent("""\
    You are an expert software engineer tasked with fixing a bug in a Python codebase.

    Your goal: understand the bug described in the problem statement and produce a fix.

    Workflow:
    1. Read the problem statement carefully.
    2. Explore the codebase to understand the relevant code.
    3. Identify the root cause.
    4. Apply the minimal fix required.
    5. When done, call the `submit` tool with a summary of what you changed.

    Rules:
    - Make minimal, targeted changes. Do not refactor unrelated code.
    - Only modify files that are necessary to fix the bug.
    - Do not run the test suite — focus on understanding and fixing the root cause.
    - When you are confident in the fix, call `submit`.
""")


@dataclass
class AgentResult:
    instance_id: str
    path: str                    # "a" or "b"
    patch: str                   # unified diff of changes made
    tool_calls: list[dict]       # names + args of every tool call
    iterations: int
    submitted: bool              # did the agent call submit, or did we hit max_iterations?
    error: str | None = None


def run_agent(
    task: BenchmarkTask,
    ctx: RepoContext,
    path: str,                   # "a" or "b"
    phronosis_base_url: str = "",     # Phronosis server URL for Path B
) -> AgentResult:
    """Run one autonomous agent session and return the produced patch."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    tools = _build_tools(path, ctx, phronosis_base_url)

    initial_message = f"""Please fix the following bug:

**Repository:** {task.repo}
**Problem:**

{task.problem_statement}

The repository is checked out at: {ctx.repo_path}

Use the available tools to explore the codebase, understand the bug, and apply a fix.
When you are confident the fix is correct, call the `submit` tool.
"""

    messages = [{"role": "user", "content": initial_message}]
    tool_calls_log: list[dict] = []
    submitted = False

    for iteration in range(MAX_ITERATIONS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        # Append assistant message
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason == "end_turn":
            break

        if response.stop_reason == "tool_use":
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                tool_calls_log.append({"name": block.name, "input": block.input})

                if block.name == "submit":
                    submitted = True
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": "Fix submitted. Session complete.",
                    })
                    break

                result = _execute_tool(block.name, block.input, ctx)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": str(result)[:8000],  # cap very large outputs
                })

            messages.append({"role": "user", "content": tool_results})

            if submitted:
                break

    patch = _compute_patch(ctx.repo_path)
    return AgentResult(
        instance_id=task.instance_id,
        path=path,
        patch=patch,
        tool_calls=tool_calls_log,
        iterations=iteration + 1,
        submitted=submitted,
    )


def _execute_tool(name: str, args: dict, ctx: RepoContext) -> str:
    """Dispatch a tool call and return its string result."""
    repo = ctx.repo_path

    if name == "read_file":
        path = _safe_path(args["path"], repo)
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return f"Error reading file: {exc}"

    if name == "write_file":
        path = _safe_path(args["path"], repo)
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(args["content"], encoding="utf-8")
            return f"Written: {path}"
        except Exception as exc:
            return f"Error writing file: {exc}"

    if name == "bash":
        try:
            result = subprocess.run(
                args["command"],
                shell=True,
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
            )
            output = result.stdout + result.stderr
            return output[:4000] if output else "(no output)"
        except subprocess.TimeoutExpired:
            return "Command timed out after 30 seconds."
        except Exception as exc:
            return f"Error: {exc}"

    if name.startswith("phronosis_"):
        return _call_phronosis_tool(name, args, ctx)

    return f"Unknown tool: {name}"


def _call_phronosis_tool(name: str, args: dict, ctx: RepoContext) -> str:
    """Call an Phronosis MCP tool via HTTP."""
    import json
    import urllib.request

    base_url = os.getenv("PHRONOSIS_URL", "http://localhost:3004")
    tool_name = name[len("phronosis_"):]  # strip prefix
    args_with_project = {**args, "project_id": ctx.project_id}

    payload = json.dumps({"tool": tool_name, "arguments": args_with_project}).encode()
    req = urllib.request.Request(
        f"{base_url}/mcp/tool/{tool_name}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode()
    except Exception as exc:
        return f"Phronosis tool error: {exc}"


def _compute_patch(repo_path: str) -> str:
    """Return unified diff of all changes made to the repo."""
    result = subprocess.run(
        ["git", "diff", "--unified=3"],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _safe_path(path: str, repo_root: str) -> str:
    """Resolve path relative to repo root; block directory traversal."""
    resolved = Path(repo_root) / path
    resolved = resolved.resolve()
    if not str(resolved).startswith(str(Path(repo_root).resolve())):
        raise ValueError(f"Path traversal blocked: {path}")
    return str(resolved)


def _build_tools(path: str, ctx: RepoContext, phronosis_base_url: str) -> list[dict]:
    """Build the tool list for the given path (A = baseline, B = Phronosis)."""
    tools = [
        {
            "name": "read_file",
            "description": "Read a file from the repository.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root"}
                },
                "required": ["path"],
            },
        },
        {
            "name": "write_file",
            "description": "Write or overwrite a file in the repository.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to repo root"},
                    "content": {"type": "string", "description": "Full file content"},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "bash",
            "description": "Run a shell command in the repo root (grep, find, cat, ls). Do NOT run tests.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                },
                "required": ["command"],
            },
        },
        {
            "name": "submit",
            "description": "Submit your fix. Call this when you are confident the bug is fixed.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "Brief description of the fix"},
                },
                "required": ["summary"],
            },
        },
    ]

    if path == "b" and ctx.phronosis_indexed:
        tools += [
            {
                "name": "phronosis_query_similar_functions",
                "description": "Semantic search: find functions similar to a description or code snippet.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "snippet": {"type": "string"},
                        "top_k": {"type": "integer", "default": 8},
                    },
                    "required": ["snippet"],
                },
            },
            {
                "name": "phronosis_get_callers",
                "description": "Return all functions that call the given function name.",
                "input_schema": {
                    "type": "object",
                    "properties": {"function_name": {"type": "string"}},
                    "required": ["function_name"],
                },
            },
            {
                "name": "phronosis_get_callees",
                "description": "Return all functions called by the given function.",
                "input_schema": {
                    "type": "object",
                    "properties": {"function_name": {"type": "string"}},
                    "required": ["function_name"],
                },
            },
            {
                "name": "phronosis_get_impact_radius",
                "description": "Return functions impacted by a change to the given function (BFS, 2 levels).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "function_name": {"type": "string"},
                        "depth": {"type": "integer", "default": 2},
                    },
                    "required": ["function_name"],
                },
            },
        ]

    return tools
