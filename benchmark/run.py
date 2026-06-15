#!/usr/bin/env python3
"""
Run the Phronosis SWE-bench benchmark.

Usage:
    # Dry run — show tasks without running agents
    python -m benchmark.run --dry-run

    # Run Path A (baseline) only
    python -m benchmark.run --path a

    # Run Path B (Phronosis) only
    python -m benchmark.run --path b

    # Run both paths (full benchmark)
    python -m benchmark.run

    # Limit to first N tasks
    python -m benchmark.run --limit 3

Options:
    --repo REPO        SWE-bench repo to benchmark (default: pytest-dev/pytest)
    --results-dir DIR  Output directory for results (default: results/)
    --phronosis-url URL     Phronosis server URL for Path B (default: $PHRONOSIS_URL or http://localhost:3004)
    --phronosis-dsn DSN     Postgres DSN for Phronosis indexing (default: $DATABASE_URL)
    --keep-repos       Don't delete cloned repos after each task (useful for debugging)
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from benchmark.loader import load_tasks
from benchmark.repo_setup import setup_repo, cleanup_repo
from benchmark.runner import run_agent
from benchmark.evaluator import evaluate
from benchmark.report import write_task_results, write_summary, print_summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Phronosis SWE-bench benchmark")
    parser.add_argument("--repo", default="pytest-dev/pytest")
    parser.add_argument("--path", choices=["a", "b", "both"], default="both")
    parser.add_argument("--limit", type=int, default=0, help="Max tasks to run (0 = all)")
    parser.add_argument("--results-dir", default="results")
    parser.add_argument("--phronosis-url", default=os.getenv("PHRONOSIS_URL", "http://localhost:3004"))
    parser.add_argument("--phronosis-dsn", default=os.getenv("DATABASE_URL", ""))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--keep-repos", action="store_true")
    args = parser.parse_args()

    run_a = args.path in ("a", "both")
    run_b = args.path in ("b", "both")

    print("Loading SWE-bench tasks…")
    tasks = load_tasks(repo=args.repo)
    if args.limit:
        tasks = tasks[:args.limit]
    print(f"Tasks: {len(tasks)} ({args.repo})")

    if args.dry_run:
        for t in tasks:
            print(f"  {t.instance_id}  base={t.base_commit[:8]}  tests={len(t.fail_to_pass)}")
        return

    Path(args.results_dir).mkdir(exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="phronosis-bench-repos-")

    for i, task in enumerate(tasks, 1):
        print(f"\n[{i}/{len(tasks)}] {task.instance_id}")

        # --- Path A setup (no Phronosis index needed) ---
        ctx_a = setup_repo(task, phronosis_index=False, workdir=workdir)

        agent_a = None
        eval_a = None
        if run_a:
            print("  Running Path A (baseline)…")
            agent_a = run_agent(task, ctx_a, path="a")
            print(f"  Path A: {len(agent_a.tool_calls)} tool calls, submitted={agent_a.submitted}")
            eval_a = evaluate(task, agent_a, ctx_a.repo_path)
            print(f"  Path A resolved: {eval_a.resolved}")

        # --- Path B setup (with Phronosis index) ---
        agent_b = None
        eval_b = None
        if run_b:
            ctx_b = setup_repo(
                task,
                phronosis_index=True,
                phronosis_dsn=args.phronosis_dsn,
                workdir=workdir,
            )
            print("  Running Path B (Phronosis)…")
            agent_b = run_agent(task, ctx_b, path="b", phronosis_base_url=args.phronosis_url)
            print(f"  Path B: {len(agent_b.tool_calls)} tool calls, submitted={agent_b.submitted}")
            eval_b = evaluate(task, agent_b, ctx_b.repo_path)
            print(f"  Path B resolved: {eval_b.resolved}")

            if not args.keep_repos:
                cleanup_repo(ctx_b)

        if not args.keep_repos:
            cleanup_repo(ctx_a)

        # Write results (skip if we only ran one path)
        if agent_a and agent_b:
            task_dir = write_task_results(
                task, agent_a, eval_a, agent_b, eval_b,
                results_dir=args.results_dir,
            )
            print(f"  Results: {task_dir}")

    summary = write_summary(args.results_dir)
    print_summary(summary)


if __name__ == "__main__":
    main()
