#!/usr/bin/env python3
"""
Phronosis SWE-bench benchmark — setup and evaluation CLI.

This script handles the deterministic parts of the benchmark:
cloning repos, creating venvs, indexing with Phronosis, applying patches,
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

from benchmark.loader import load_tasks_chronological, load_multifile_tasks, select_calibration_tasks
from benchmark.repo_setup import setup_repo, cleanup_repo, RepoContext
from benchmark.runner import capture_patch, save_patch, AgentResult
from benchmark.evaluator import evaluate
from benchmark.report import write_task_results, write_summary, print_summary


# Global workdir shared across setup calls in one session
_WORKDIR_FILE = Path("/tmp/phronosis-bench-workdir")


def _get_or_create_workdir() -> str:
    if _WORKDIR_FILE.exists():
        wd = _WORKDIR_FILE.read_text().strip()
        if Path(wd).exists():
            return wd
    wd = tempfile.mkdtemp(prefix="phronosis-bench-")
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
    tasks = load_tasks_chronological(repo=args.repo)
    task = next((t for t in tasks if t.instance_id == args.instance_id), None)
    if not task:
        print(f"ERROR: task {args.instance_id!r} not found", file=sys.stderr)
        sys.exit(1)

    workdir = _get_or_create_workdir()
    phronosis_index = args.path == "b"

    ctx = setup_repo(
        task,
        phronosis_index=phronosis_index,
        phronosis_dsn=os.getenv("DATABASE_URL", ""),
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
        "phronosis_indexed": ctx.phronosis_indexed,
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
        # stored as count (from `metrics` subcommand) — reconstruct minimal list
        tool_calls_list = [{"name": "tool"} for _ in range(tool_calls_raw)]
    else:
        tool_calls_list = [{"name": n} for n in tool_calls_raw]

    agent_result = AgentResult(
        instance_id=task.instance_id,
        path=args.path,
        patch=patch,
        tool_calls=tool_calls_list,
        iterations=metrics.get("iterations", 0),
        submitted=bool(patch.strip()),
        agent_tokens=metrics.get("agent_tokens", 0),
    )

    result = evaluate(
        task,
        agent_result,
        ctx_data["repo_path"],
        venv_python=ctx_data["venv_python"],
    )

    out = path_dir / "evaluation.json"
    out.write_text(json.dumps({
        "resolved": result.resolved,
        "patch_applied": result.patch_applied,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "error": result.error,
        "agent_tokens": agent_result.agent_tokens,
        "tool_call_count": len(agent_result.tool_calls),
    }, indent=2))

    print(json.dumps({
        "resolved": result.resolved,
        "tests_passed": result.tests_passed,
        "tests_failed": result.tests_failed,
        "error": result.error,
    }))


def cmd_metrics(args) -> None:
    """Write agent metrics (tokens, tool calls) for one path so evaluate can pick them up."""
    path_dir = Path(args.results_dir) / args.instance_id / f"path_{args.path}"
    path_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "agent_tokens": args.tokens,
        "tool_calls": args.tool_calls,
        "iterations": args.iterations,
    }
    (path_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps({"written": str(path_dir / "metrics.json")}))


def cmd_summary(args) -> None:
    summary = write_summary(args.results_dir)
    print_summary(summary)


def main() -> None:
    parser = argparse.ArgumentParser(description="Phronosis SWE-bench benchmark CLI")
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

    p_metrics = sub.add_parser("metrics", help="Record agent token/tool metrics before evaluate")
    p_metrics.add_argument("instance_id")
    p_metrics.add_argument("--path", choices=["a", "b"], required=True)
    p_metrics.add_argument("--tokens", type=int, default=0)
    p_metrics.add_argument("--tool-calls", type=int, default=0, dest="tool_calls")
    p_metrics.add_argument("--iterations", type=int, default=0)

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "setup":
        cmd_setup(args)
    elif args.command == "evaluate":
        cmd_evaluate(args)
    elif args.command == "metrics":
        cmd_metrics(args)
    elif args.command == "summary":
        cmd_summary(args)


if __name__ == "__main__":
    main()
