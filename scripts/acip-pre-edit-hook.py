#!/usr/bin/env python3
"""
PreToolUse hook — ACIP workflow enforcement.

Fires on: Bash (grep), Read (source files), Edit (source files)

Bash:  nudge toward ACIP tools instead of grep-based exploration.
Read:  gate — blocks the first read of a session, calls ACIP itself,
       prints the architectural summary, then defers ("review the above
       then retry"). Gate valid for 30 min; subsequent reads pass silently.
Edit:  hard risk-signal check — warns on chokepoints and risk-surface
       functions with the three pre-edit ACIP calls to run first.

Gate strategy:
  - First Read of a session on a source file → hook fetches project_home,
    prints it, writes ~/.claude/acip_gates/{project_id}, exits 2 (blocks).
  - Retry Read in same session → gate exists, exits 0 (allows).
  - Gate expires after GATE_TTL seconds → next Read re-fetches and re-gates.
  - ACIP unreachable → silent pass (never block when the server is down).
"""
import json
import os
import re
import sys
import time
import urllib.request

ACIP_URL = os.environ.get("ACIP_URL", "http://localhost:3004")
TIMEOUT = 3
GATE_TTL = 1800  # 30 minutes
_GATE_DIR = os.path.expanduser("~/.claude/acip_gates")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _project_id() -> str:
    """Resolve project ID from env, git remote, or repo dirname."""
    pid = os.environ.get("ACIP_PROJECT", "")
    if pid:
        return pid
    try:
        import subprocess
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
        return re.sub(r"\.git$", "", remote).split("/")[-1].split(":")[-1]
    except Exception:
        pass
    try:
        import subprocess
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
        return os.path.basename(root)
    except Exception:
        return ""


def _get_project_home(project_id: str) -> dict:
    """Fetch the ACIP project home snapshot; returns empty dict on any error."""
    try:
        safe = urllib.request.quote(project_id, safe="")
        url = f"{ACIP_URL}/api/project-home/{safe}"
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception:
        return {}


def _file_to_module(file_path: str) -> str:
    """Convert src/call_graph/storage.py -> src.call_graph.storage"""
    p = file_path
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
        if p.endswith(ext):
            p = p[: -len(ext)]
    return p.replace("/", ".").lstrip(".")


def _gate_path(project_id: str) -> str:
    """Return the path to the gate file for a project."""
    os.makedirs(_GATE_DIR, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", project_id)
    return os.path.join(_GATE_DIR, safe)


def _gate_valid(project_id: str) -> bool:
    """Return True if a fresh gate exists for this project."""
    try:
        return (time.time() - os.path.getmtime(_gate_path(project_id))) < GATE_TTL
    except FileNotFoundError:
        return False


def _write_gate(project_id: str) -> None:
    """Write or refresh the gate file for a project."""
    open(_gate_path(project_id), "w").write(str(time.time()))


def _fmt_ids(items: list, key: str = "id", n: int = 3) -> str:
    """Format a short list of IDs for display."""
    names = [".".join(i.get(key, "").split(".")[-2:]) for i in items[:n]]
    return ", ".join(names) if names else "none"


# ── Main ──────────────────────────────────────────────────────────────────────

try:
    data = json.loads(sys.stdin.read())
    tool = data.get("tool_name", "")
    inp = data.get("tool_input", {})

    # ── Bash: nudge on grep against source files ──────────────────────────────
    if tool == "Bash":
        cmd = inp.get("command", "")
        if (
            re.search(r"\bgrep\b", cmd)
            and re.search(r"\.(py|ts|tsx|js|jsx)", cmd)
            and not re.search(r"\b(git|pytest|rtk|ruff|mypy|test)\b", cmd)
        ):
            print(
                "[ACIP] grep on source — MCP is faster and cross-file:\n"
                "  get_callers(fn) · get_callees(fn) · query_similar_functions(snippet)"
            )

    # ── Read: gate — fetch ACIP context on first access, block until seen ─────
    elif tool == "Read":
        path = inp.get("file_path", "")
        if not re.search(r"\.(py|ts|tsx|js|jsx)$", path):
            sys.exit(0)
        if "/scripts/" in path or "/test" in path or "/__" in path:
            sys.exit(0)

        project_id = _project_id()
        if not project_id:
            sys.exit(0)

        if _gate_valid(project_id):
            # Gate is fresh — pass silently
            sys.exit(0)

        # Gate expired or absent — fetch ACIP context, display it, then block.
        home = _get_project_home(project_id)
        if not home:
            # ACIP unreachable — nudge only, never hard-block
            print(
                "[ACIP] Reading source — if exploring structure, MCP is faster:\n"
                "  get_impact_radius(fn) · get_decision_history(fn) · get_callers(fn)"
            )
            sys.exit(0)

        # Print architectural summary so the agent actually sees it
        print(f"[ACIP] Architectural context — {project_id} "
              f"({home.get('function_count', '?')} functions)")
        print(f"  Chokepoints : {_fmt_ids(home.get('chokepoints', []))}")
        print(f"  Risk surface: {_fmt_ids(home.get('risk_surface', []))}")
        print(f"  Entry points: {_fmt_ids(home.get('entry_points', []))}")

        ssl = home.get("since_last_session")
        if ssl and any(ssl.get(k) for k in ("functions_added", "functions_modified", "functions_removed")):
            added = len(ssl.get("functions_added", []))
            modified = len(ssl.get("functions_modified", []))
            removed = len(ssl.get("functions_removed", []))
            print(f"  Since last session: +{added} ~{modified} -{removed} functions")

        gaps = home.get("health", {}).get("top_knowledge_gaps", [])
        if gaps:
            print(f"  Top knowledge gap: {gaps[0].get('id', '').split('.')[-1]} "
                  f"({gaps[0].get('caller_count', 0)} callers, no docstring/decisions)")

        print()
        print("[ACIP] Context loaded. Retry your Read — this message won't repeat "
              f"for {GATE_TTL // 60} minutes.")

        # Write gate so the immediate retry passes
        _write_gate(project_id)
        sys.exit(2)  # Block this attempt; next attempt passes

    # ── Edit: risk-signal check before modifying source ───────────────────────
    elif tool == "Edit":
        path = inp.get("file_path", "")
        if not re.search(r"\.(py|ts|tsx|js|jsx)$", path):
            sys.exit(0)

        module = _file_to_module(path)
        project_id = _project_id()
        if not project_id:
            sys.exit(0)

        home = _get_project_home(project_id)
        if not home:
            sys.exit(0)

        # A successful Edit-time ACIP call also refreshes the gate
        _write_gate(project_id)

        warnings = []

        for cp in home.get("chokepoints", []):
            fid = cp.get("id", "")
            if module and (module in fid or fid.startswith(module)):
                warnings.append(
                    f"  CHOKEPOINT  {'.'.join(fid.split('.')[-2:])}  "
                    f"({cp['caller_count']} callers — signature changes break everything depending on it)"
                )

        for rs in home.get("risk_surface", []):
            fid = rs.get("id", "")
            if module and (module in fid or fid.startswith(module)):
                warnings.append(
                    f"  RISK SURFACE  {'.'.join(fid.split('.')[-2:])}  "
                    f"({rs['churn']} patches · {rs['caller_count']} callers — high-churn AND high-impact)"
                )

        if warnings:
            print(f"[ACIP] High-risk edit in {os.path.basename(path)}:")
            for w in warnings:
                print(w)
            print("  Before editing, run:")
            print("  1. get_impact_radius(fn, depth=2)        — what breaks?")
            print("  2. get_decision_history(fn)              — why was this designed this way?")
            print("  3. query_similar_functions(snippet)      — what is the existing pattern?")
        else:
            fn_hint = module.split(".")[-1] if module else "fn"
            print(
                f"[ACIP] Pre-edit: get_impact_radius({fn_hint}) · "
                f"get_decision_history({fn_hint}) · "
                f"query_similar_functions(snippet)"
            )

except Exception:
    pass  # Never block Claude on a hook error

sys.exit(0)
