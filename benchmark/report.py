"""
Write per-task result JSON files and generate the comparison summary.

Results directory layout (public artifact):
    results/
        {instance_id}/
            task.json           — problem statement + test IDs (no ground truth patch)
            path_a/
                patch.diff      — what the baseline agent produced
                evaluation.json — pass/fail per test
            path_b/
                patch.diff      — what the Scopenos-assisted agent produced
                evaluation.json — pass/fail per test
        summary.json            — aggregate pass rates
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .loader import BenchmarkTask
from .runner import AgentResult
from .evaluator import EvaluationResult


def write_task_results(
    task: BenchmarkTask,
    agent_a: AgentResult,
    eval_a: EvaluationResult,
    agent_b: AgentResult,
    eval_b: EvaluationResult,
    results_dir: str = "results",
) -> Path:
    """Write all artifacts for one task. Returns the task result directory."""
    task_dir = Path(results_dir) / task.instance_id
    task_dir.mkdir(parents=True, exist_ok=True)

    # task.json — public, no ground truth
    (task_dir / "task.json").write_text(json.dumps({
        "instance_id": task.instance_id,
        "repo": task.repo,
        "base_commit": task.base_commit,
        "problem_statement": task.problem_statement,
        "fail_to_pass": task.fail_to_pass,
        "pass_to_pass": task.pass_to_pass,
    }, indent=2))

    for agent, evaluation in [("path_a", (agent_a, eval_a)), ("path_b", (agent_b, eval_b))]:
        ar, er = evaluation
        path_dir = task_dir / agent
        path_dir.mkdir(exist_ok=True)

        (path_dir / "patch.diff").write_text(ar.patch or "")
        (path_dir / "evaluation.json").write_text(json.dumps({
            "patch_applied": er.patch_applied,
            "resolved": er.resolved,
            "tests_passed": er.tests_passed,
            "tests_failed": er.tests_failed,
            "error": er.error,
            "tool_call_count": len(ar.tool_calls),
            "tool_calls": [t["name"] for t in ar.tool_calls],
            "iterations": ar.iterations,
            "submitted": ar.submitted,
        }, indent=2))

    return task_dir


def write_summary(results_dir: str = "results") -> dict:
    """
    Scan all task directories and produce aggregate summary.json.
    Returns the summary dict.
    """
    results_path = Path(results_dir)
    tasks_a_resolved = 0
    tasks_b_resolved = 0
    total_a_tokens = 0
    total_b_tokens = 0
    total_a_tools = 0
    total_b_tools = 0
    total = 0

    rows = []
    for task_dir in sorted(results_path.iterdir()):
        if not task_dir.is_dir() or task_dir.name == "summary.json":
            continue
        eval_a = _load_eval(task_dir / "path_a" / "evaluation.json")
        eval_b = _load_eval(task_dir / "path_b" / "evaluation.json")
        if eval_a is None and eval_b is None:
            continue

        total += 1
        a_ok = eval_a.get("resolved", False) if eval_a else False
        b_ok = eval_b.get("resolved", False) if eval_b else False
        a_tokens = eval_a.get("agent_tokens", 0) if eval_a else 0
        b_tokens = eval_b.get("agent_tokens", 0) if eval_b else 0
        a_tools = eval_a.get("tool_call_count", 0) if eval_a else 0
        b_tools = eval_b.get("tool_call_count", 0) if eval_b else 0

        if a_ok:
            tasks_a_resolved += 1
        if b_ok:
            tasks_b_resolved += 1
        total_a_tokens += a_tokens
        total_b_tokens += b_tokens
        total_a_tools += a_tools
        total_b_tools += b_tools

        rows.append({
            "instance_id": task_dir.name,
            "path_a_resolved": a_ok,
            "path_b_resolved": b_ok,
            "scopenos_advantage": b_ok and not a_ok,
            "path_a_tokens": a_tokens,
            "path_b_tokens": b_tokens,
            "path_a_tools": a_tools,
            "path_b_tools": b_tools,
            "token_savings": a_tokens - b_tokens,
        })

    summary = {
        "total_tasks": total,
        "path_a": {
            "resolved": tasks_a_resolved,
            "resolve_rate": round(tasks_a_resolved / total, 4) if total else 0,
            "total_tokens": total_a_tokens,
            "total_tool_calls": total_a_tools,
        },
        "path_b": {
            "resolved": tasks_b_resolved,
            "resolve_rate": round(tasks_b_resolved / total, 4) if total else 0,
            "total_tokens": total_b_tokens,
            "total_tool_calls": total_b_tools,
        },
        "token_savings": total_a_tokens - total_b_tokens,
        "token_savings_pct": round((total_a_tokens - total_b_tokens) / total_a_tokens * 100, 1) if total_a_tokens else 0,
        "scopenos_advantage_tasks": sum(1 for r in rows if r["scopenos_advantage"]),
        "tasks": rows,
    }

    (results_path / "summary.json").write_text(json.dumps(summary, indent=2))
    return summary


def print_summary(summary: dict) -> None:
    total = summary["total_tasks"]
    a = summary["path_a"]
    b = summary["path_b"]
    tok_save = summary.get("token_savings", 0)
    tok_pct = summary.get("token_savings_pct", 0)
    print(f"\n{'='*56}")
    print(f"  Benchmark Results ({total} tasks)")
    print(f"{'='*56}")
    print(f"  {'Metric':<28} {'Path A':>10} {'Path B':>10}")
    print(f"  {'-'*48}")
    print(f"  {'Resolved':<28} {a['resolved']:>9}/{total} {b['resolved']:>9}/{total}")
    if a['total_tokens'] or b['total_tokens']:
        print(f"  {'Total tokens':<28} {a['total_tokens']:>10,} {b['total_tokens']:>10,}")
        print(f"  {'Token savings (B vs A)':<28} {tok_save:>+10,} ({tok_pct:+.1f}%)")
    if a['total_tool_calls'] or b['total_tool_calls']:
        tool_delta = b['total_tool_calls'] - a['total_tool_calls']
        print(f"  {'Total tool calls':<28} {a['total_tool_calls']:>10,} {b['total_tool_calls']:>10,}  ({tool_delta:+d})")
    print(f"  {'-'*48}")
    print(f"  Scopenos advantage: {summary['scopenos_advantage_tasks']} task(s) Path B resolved that A could not")
    print(f"{'='*56}\n")


def _load_eval(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except Exception:
        return None
