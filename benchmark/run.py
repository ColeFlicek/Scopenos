#!/usr/bin/env python3
"""
Scopenos SWE-bench benchmark — setup and evaluation CLI.

This script handles the deterministic parts of the benchmark:
cloning repos, creating venvs, indexing with Scopenos, applying patches,
running tests, and writing results.

The Claude Code session handles spawning the actual agent subagents
(Path A and Path B) and capturing their diffs.

Usage:
    # Show what tasks would run
    python -m benchmark.run --dry-run
    python -m benchmark.run --calibration --dry-run

    # Set up a single task environment (called by the Claude Code orchestrator)
    python -m benchmark.run setup <instance_id> [--path a|b] [--results-dir results]

    # Evaluate a patch already written to results/{instance_id}/path_{a|b}/patch.diff
    python -m benchmark.run evaluate <instance_id> --path a|b [--results-dir results]

    # Print summary of completed results
    python -m benchmark.run summary [--results-dir results]

    # List tasks (for the orchestrator to iterate over)
    python -m benchmark.run list [--calibration] [--repo pytest-dev/pytest]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmark.loader import load_tasks_chronological, load_multifile_tasks, load_path_a_hard_tasks, select_calibration_tasks
from benchmark.repo_setup import setup_repo, cleanup_repo, RepoContext
from benchmark.runner import capture_patch, save_patch, AgentResult, parse_tool_log
from benchmark.evaluator import evaluate
from benchmark.report import write_task_results, write_summary, print_summary


# Global workdir shared across setup calls in one session
_WORKDIR_FILE = Path("/tmp/scopenos-bench-workdir")


def _get_or_create_workdir() -> str:
    if _WORKDIR_FILE.exists():
        wd = _WORKDIR_FILE.read_text().strip()
        if Path(wd).exists():
            return wd
    wd = tempfile.mkdtemp(prefix="scopenos-bench-")
    _WORKDIR_FILE.write_text(wd)
    return wd


def cmd_list(args) -> None:
    if getattr(args, "multifile", False):
        tasks = load_multifile_tasks(max_tasks=args.max_tasks)
    else:
        tasks = load_tasks_chronological(repo=args.repo)
        if args.calibration:
            tasks = select_calibration_tasks(tasks)
    for t in tasks:
        print(json.dumps({
            "instance_id": t.instance_id,
            "repo": t.repo,
            "base_commit": t.base_commit,
            "fail_to_pass_count": len(t.fail_to_pass),
        }))


def cmd_setup(args) -> None:
    # Check saved task.json first — avoids a slow HF download for Full-only tasks.
    task = None
    saved = Path(args.results_dir) / args.instance_id / "task.json"
    if saved.exists():
        from benchmark.loader import BenchmarkTask
        data = json.loads(saved.read_text())
        task = BenchmarkTask(
            instance_id=data["instance_id"],
            repo=data["repo"],
            base_commit=data["base_commit"],
            problem_statement=data["problem_statement"],
            fail_to_pass=data["fail_to_pass"],
            pass_to_pass=data.get("pass_to_pass", []),
        )
    if task is None:
        tasks = load_tasks_chronological(repo=args.repo)
        task = next((t for t in tasks if t.instance_id == args.instance_id), None)
    if not task:
        print(f"ERROR: task {args.instance_id!r} not found", file=sys.stderr)
        sys.exit(1)

    workdir = _get_or_create_workdir()
    scopenos_index = args.path == "b"

    ctx = setup_repo(
        task,
        scopenos_index=scopenos_index,
        scopenos_dsn=os.getenv("DATABASE_URL", ""),
        workdir=workdir,
    )

    # Write context for the orchestrator to consume
    ctx_file = Path(args.results_dir) / task.instance_id / f"path_{args.path}" / "ctx.json"
    ctx_file.parent.mkdir(parents=True, exist_ok=True)
    ctx_file.write_text(json.dumps({
        "instance_id": task.instance_id,
        "path": args.path,
        "repo_path": ctx.repo_path,
        "venv_python": ctx.venv_python,
        "project_id": ctx.project_id,
        "scopenos_indexed": ctx.scopenos_indexed,
        "fail_to_pass": task.fail_to_pass,
    }, indent=2))

    print(json.dumps({
        "status": "ready",
        "repo_path": ctx.repo_path,
        "venv_python": ctx.venv_python,
        "project_id": ctx.project_id,
        "ctx_file": str(ctx_file),
    }))


def cmd_evaluate(args) -> None:
    tasks = load_tasks_chronological(repo=args.repo)
    task = next((t for t in tasks if t.instance_id == args.instance_id), None)
    if not task:
        print(f"ERROR: task {args.instance_id!r} not found", file=sys.stderr)
        sys.exit(1)

    path_dir = Path(args.results_dir) / task.instance_id / f"path_{args.path}"
    ctx_file = path_dir / "ctx.json"
    patch_file = path_dir / "patch.diff"

    if not ctx_file.exists():
        print(f"ERROR: ctx.json not found — run setup first", file=sys.stderr)
        sys.exit(1)

    ctx_data = json.loads(ctx_file.read_text())
    patch = patch_file.read_text() if patch_file.exists() else ""

    # Read metrics written by orchestrator (token counts, tool call counts)
    metrics_file = path_dir / "metrics.json"
    metrics = json.loads(metrics_file.read_text()) if metrics_file.exists() else {}

    tool_calls_raw = metrics.get("tool_calls", [])
    if isinstance(tool_calls_raw, int):
        tool_calls_list = [{"tool": "unknown"} for _ in range(tool_calls_raw)]
    elif isinstance(tool_calls_raw, list) and tool_calls_raw and isinstance(tool_calls_raw[0], str):
        # legacy: plain list of names
        tool_calls_list = [{"tool": n, "reason": ""} for n in tool_calls_raw]
    else:
        tool_calls_list = tool_calls_raw  # already list[dict]

    agent_result = AgentResult(
        instance_id=task.instance_id,
        path=args.path,
        patch=patch,
        tool_calls=tool_calls_list,
        iterations=metrics.get("iterations", 0),
        submitted=bool(patch.strip()),
        notes=metrics.get("notes", ""),
        agent_tokens=metrics.get("agent_tokens", 0),
    )

    result = evaluate(
        task,
        agent_result,
        ctx_data["repo_path"],
        venv_python=ctx_data["venv_python"],
    )

    # Per-tool breakdown for analysis
    scopenos_tools = {
        "get_project_home", "query_similar_functions", "get_impact_radius",
        "get_callers", "get_callees", "get_subsystem_detail",
        "get_decision_history", "search_decisions",
    }
    tool_names = [e.get("tool", "unknown") for e in agent_result.tool_calls]
    scopenos_calls = [e for e in agent_result.tool_calls if e.get("tool") in scopenos_tools]
    file_read_calls = [e for e in agent_result.tool_calls if e.get("tool") in ("Read", "Bash")]

    out = path_dir / "evaluation.json"
    out.write_text(json.dumps({
        "resolved": result.resolved,
        "patch_applied": result.patch_applied,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "error": result.error,
        "agent_tokens": agent_result.agent_tokens,
        "tool_call_count": len(agent_result.tool_calls),
        "scopenos_call_count": len(scopenos_calls),
        "file_read_count": len(file_read_calls),
        "notes": agent_result.notes,
        "tool_log": agent_result.tool_calls,
    }, indent=2))

    print(json.dumps({
        "resolved": result.resolved,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "error": result.error,
        "scopenos_calls": len(scopenos_calls),
        "file_reads": len(file_read_calls),
    }))


def cmd_metrics(args) -> None:
    """Write agent metrics (tokens, tool calls, notes) for one path so evaluate can pick them up."""
    path_dir = Path(args.results_dir) / args.instance_id / f"path_{args.path}"
    path_dir.mkdir(parents=True, exist_ok=True)
    raw = args.tool_calls
    try:
        tool_calls = json.loads(raw)  # JSON array — either strings or {tool, reason, ...} dicts
    except (json.JSONDecodeError, TypeError):
        tool_calls = int(raw)         # plain int count (backwards compat)

    # If the orchestrator passes raw agent output instead of extracted JSON, parse it
    if isinstance(tool_calls, str):
        parsed, notes = parse_tool_log(tool_calls)
        tool_calls = parsed
    else:
        notes = args.notes

    metrics = {
        "agent_tokens": args.tokens,
        "tool_calls": tool_calls,
        "iterations": args.iterations,
        "notes": notes,
    }
    (path_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps({"written": str(path_dir / "metrics.json")}))


def cmd_summary(args) -> None:
    summary = write_summary(args.results_dir)
    print_summary(summary)


# ── MCP connectivity check ─────────────────────────────────────────────────────

def cmd_check_mcp(args) -> None:
    """
    Simulate the exact MCP handshake a Claude Code subagent performs.

    A passing run proves that subagents will have Scopenos tools available.
    A failing run blocks the benchmark with a clear diagnostic so you don't
    waste tokens on a Path B run that silently degrades to grep.

    Steps mirror the Claude Code MCP client lifecycle:
      1. POST /mcp  initialize          → must return mcp-session-id header
      2. POST /mcp  tools/list          → must list ≥1 Scopenos tool
      3. POST /mcp  tools/call          → list_projects must return valid JSON

    Note: notifications/initialized is intentionally omitted. Real Claude Code
    clients open a GET SSE stream for server-to-client notifications rather than
    POSTing the notification. POSTing it in stateful mode causes the server to
    terminate the session, which would make this test a false negative.
    """
    import http.client
    import urllib.parse

    url = args.mcp_url
    api_key = args.api_key or os.getenv("SCOPENOS_API_KEY", "")

    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    path = parsed.path or "/mcp"

    failures: list[str] = []
    session_id: str = ""

    def post(method: str, params: dict | None = None, sid: str = "") -> tuple[int, dict, dict]:
        """Return (status, headers_dict, body_dict). body_dict is {} on parse failure."""
        payload = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": method,
            "params": params or {},
        }).encode()
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Content-Length": str(len(payload)),
        }
        if api_key:
            hdrs["X-API-Key"] = api_key
        if sid:
            hdrs["mcp-session-id"] = sid

        conn = http.client.HTTPConnection(host, port, timeout=10)
        conn.request("POST", path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        status = resp.status
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        raw = resp.read().decode(errors="replace")
        conn.close()

        # SSE wraps the JSON: "event: message\ndata: {...}"
        body: dict = {}
        for line in raw.splitlines():
            if line.startswith("data:"):
                try:
                    body = json.loads(line[5:].strip())
                    break
                except json.JSONDecodeError:
                    pass
        if not body:
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                pass
        return status, resp_headers, body

    ok = "\033[32m✔\033[0m" if not args.no_color else "OK"
    fail = "\033[31m✘\033[0m" if not args.no_color else "FAIL"

    print(f"MCP connectivity check → {url}")
    print()

    # ── Step 1: initialize ────────────────────────────────────────────────────
    print(f"  [1/4] initialize ...", end=" ", flush=True)
    try:
        status, hdrs, body = post("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "scopenos-bench-check", "version": "1.0"},
        })
        if status != 200:
            print(fail)
            failures.append(f"initialize: HTTP {status}")
        elif "error" in body:
            print(fail)
            failures.append(f"initialize: {body['error']}")
        else:
            session_id = hdrs.get("mcp-session-id", "")
            if not session_id:
                print(fail)
                failures.append(
                    "initialize: no mcp-session-id header returned — server is in "
                    "stateless_http=True mode; subagents cannot maintain sessions. "
                    "Fix: set stateless_http=False and redeploy."
                )
            else:
                server_name = body.get("result", {}).get("serverInfo", {}).get("name", "?")
                print(f"{ok}  session={session_id[:12]}…  server={server_name}")
    except Exception as exc:
        print(fail)
        failures.append(f"initialize: {exc}")

    # ── Step 2: tools/list ────────────────────────────────────────────────────
    print(f"  [2/3] tools/list ...", end=" ", flush=True)
    tool_names: list[str] = []
    try:
        status, _, body = post("tools/list", {}, sid=session_id)
        tools = body.get("result", {}).get("tools", [])
        tool_names = [t.get("name", "") for t in tools]
        scopenos_tools = [n for n in tool_names if n in {
            "get_project_home", "query_similar_functions", "get_impact_radius",
            "get_callers", "get_callees", "get_decision_history", "list_projects",
        }]
        if not scopenos_tools:
            print(fail)
            failures.append(
                f"tools/list: no Scopenos tools in response "
                f"(got {len(tool_names)} tools total: {tool_names[:5]})"
            )
        else:
            print(f"{ok}  {len(scopenos_tools)} Scopenos tools visible")
    except Exception as exc:
        print(fail)
        failures.append(f"tools/list: {exc}")

    # ── Step 3: tool call ─────────────────────────────────────────────────────
    print(f"  [3/3] tools/call list_projects ...", end=" ", flush=True)
    try:
        status, _, body = post("tools/call", {
            "name": "list_projects",
            "arguments": {},
        }, sid=session_id)
        if "error" in body:
            print(fail)
            failures.append(f"tools/call: {body['error']}")
        else:
            result = body.get("result", {})
            print(f"{ok}  got result (type={type(result).__name__})")
    except Exception as exc:
        print(fail)
        failures.append(f"tools/call: {exc}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if failures:
        print(f"  {fail}  MCP check FAILED ({len(failures)} issue(s)) — Path B benchmark blocked")
        print()
        for f in failures:
            print(f"     • {f}")
        print()
        print("  Subagents will NOT have Scopenos tools. Fix the above before")
        print("  running Path B or results will be identical to Path A.")
        sys.exit(1)
    else:
        print(f"  {ok}  MCP check PASSED — subagents will have Scopenos tools")
        sys.exit(0)


def cmd_weekly_tasks(args) -> None:
    """Pick a balanced set of tasks for a weekly autonomous benchmark session."""
    from benchmark.loader import _PATH_A_HARD_TASKS
    import random

    categories = args.categories or list(_PATH_A_HARD_TASKS.keys())

    # Try to load full task metadata from SWE-bench (needs `datasets` installed).
    # Falls back to listing task IDs only if the library isn't available.
    try:
        from benchmark.loader import load_path_a_hard_tasks
        tasks = load_path_a_hard_tasks(categories=categories)
        if args.repos:
            tasks = [t for t in tasks if t.repo in set(args.repos)]

        random.seed(args.seed)
        if len(tasks) <= args.n:
            selected = tasks
        else:
            per_cat = max(1, args.n // len(categories))
            selected = []
            for cat in categories:
                cat_tasks = load_path_a_hard_tasks(categories=[cat])
                if args.repos:
                    cat_tasks = [t for t in cat_tasks if t.repo in set(args.repos)]
                selected.extend(random.sample(cat_tasks, min(per_cat, len(cat_tasks))))
            selected = selected[:args.n]

        print(f"# Weekly benchmark — {len(selected)} tasks ({', '.join(categories)})")
        print(f"# Seed: {args.seed}  Repos: {list({t.repo for t in selected})}")
        print()
        for t in selected:
            print(json.dumps({
                "instance_id": t.instance_id,
                "repo": t.repo,
                "base_commit": t.base_commit[:8],
                "fail_to_pass_count": len(t.fail_to_pass),
            }))

    except ImportError:
        # No `datasets` — just list available task IDs from the hardcoded manifest
        all_ids: list[str] = []
        for cat in categories:
            all_ids.extend(_PATH_A_HARD_TASKS.get(cat, []))
        if args.repos:
            # Can't filter by repo without metadata — show all and let user pick
            print("# WARNING: install `datasets` to filter by repo. Showing all IDs.")
        random.seed(args.seed)
        random.shuffle(all_ids)
        selected_ids = all_ids[:args.n]
        print(f"# Weekly benchmark — {len(selected_ids)} tasks ({', '.join(categories)})")
        print(f"# Seed: {args.seed}  (install `datasets` for full metadata)")
        print()
        for iid in selected_ids:
            print(json.dumps({"instance_id": iid}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Scopenos SWE-bench benchmark CLI")
    parser.add_argument("--repo", default="pytest-dev/pytest")
    parser.add_argument("--results-dir", default="benchmark/results")

    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List tasks (JSON lines)")
    p_list.add_argument("--calibration", action="store_true")
    p_list.add_argument("--multifile", action="store_true", help="Load multi-file tasks from SWE-bench Full")
    p_list.add_argument("--max-tasks", type=int, default=20)

    p_setup = sub.add_parser("setup", help="Clone + venv + index one task")
    p_setup.add_argument("instance_id")
    p_setup.add_argument("--path", choices=["a", "b"], required=True)

    p_eval = sub.add_parser("evaluate", help="Apply patch and run tests")
    p_eval.add_argument("instance_id")
    p_eval.add_argument("--path", choices=["a", "b"], required=True)

    sub.add_parser("summary", help="Print aggregate results")

    p_weekly = sub.add_parser("weekly-tasks", help="Pick a balanced set of tasks for a weekly session")
    p_weekly.add_argument("--n", type=int, default=5, help="Number of tasks to pick (default: 5)")
    p_weekly.add_argument("--categories", nargs="+", choices=["protocol_pair", "visitor_pattern", "sibling_class"],
                          help="Limit to these categories (default: all)")
    p_weekly.add_argument("--repos", nargs="+", metavar="REPO",
                          help="Limit to these repos (e.g. django/django pytest-dev/pytest)")
    p_weekly.add_argument("--seed", type=int, default=42, help="Random seed for reproducible picks")

    p_check = sub.add_parser(
        "check-mcp",
        help="Verify MCP subagent connectivity before running Path B benchmarks",
    )
    p_check.add_argument(
        "--mcp-url",
        default=os.getenv("SCOPENOS_URL", "http://100.71.88.106:3004") + "/mcp",
        help="MCP endpoint to test (default: $SCOPENOS_URL/mcp)",
    )
    p_check.add_argument(
        "--api-key",
        default=None,
        help="X-API-Key value (default: $SCOPENOS_API_KEY)",
    )
    p_check.add_argument(
        "--no-color",
        action="store_true",
        help="Plain ASCII output (for CI logs)",
    )

    p_metrics = sub.add_parser("metrics", help="Record agent token/tool metrics before evaluate")
    p_metrics.add_argument("instance_id")
    p_metrics.add_argument("--path", choices=["a", "b"], required=True)
    p_metrics.add_argument("--tokens", type=int, default=0)
    p_metrics.add_argument("--tool-calls", default="0", dest="tool_calls",
                           help="int count, JSON array of names, or JSON array of {tool,reason} dicts")
    p_metrics.add_argument("--iterations", type=int, default=0)
    p_metrics.add_argument("--notes", default="",
                           help="agent's one-sentence summary of what it found")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "evaluate":
        cmd_evaluate(args)
    elif args.command == "metrics":
        cmd_metrics(args)
    elif args.command == "check-mcp":
        cmd_check_mcp(args)
    elif args.command == "summary":
        cmd_summary(args)
    elif args.command == "weekly-tasks":
        cmd_weekly_tasks(args)


if __name__ == "__main__":
    main()
