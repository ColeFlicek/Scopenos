#!/usr/bin/env python3
"""
Backfill Scopenos decision memory from git history.

Reads every commit in the current repo and POSTs it to the Scopenos
/api/decisions endpoint. Each commit message becomes a decision record
with the commit hash as its trigger.

Usage:
    SCOPENOS_URL=http://100.71.88.106:3004 python3 scripts/backfill_decisions.py
    python3 scripts/backfill_decisions.py --dry-run
    python3 scripts/backfill_decisions.py --since 2024-01-01
    python3 scripts/backfill_decisions.py --limit 50
    python3 scripts/backfill_decisions.py --project myapp
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen
import os


# ── Config ─────────────────────────────────────────────────────────────────────

SCOPENOS_URL = os.environ.get("SCOPENOS_URL", "http://100.71.88.106:3004")
SCOPENOS_API_KEY = os.environ.get("SCOPENOS_API_KEY", "")
DELAY_BETWEEN_CALLS = 0.5  # seconds — each call embeds via OpenAI/Ollama


# ── Type classifier ────────────────────────────────────────────────────────────

def classify(subject: str) -> str:
    """Map a commit subject line to an Scopenos decision type based on its leading verb."""
    low = subject.lower()
    if low.startswith(("fix", "bug", "patch", "hotfix", "revert")):
        return "Patch"
    if low.startswith(("add", "feat", "impl", "build", "create", "new", "support")):
        return "Implementation"
    if low.startswith(("refactor", "redesign", "move", "extract", "restructure", "rename", "clean", "drop", "remove")):
        return "Design"
    if low.startswith(("arch",)):
        return "Architectural"
    return "Patch"


# ── Git helpers ────────────────────────────────────────────────────────────────

def git(*args: str) -> str:
    """Run a git subcommand and return its stdout as a stripped string."""
    return subprocess.check_output(["git"] + list(args), stderr=subprocess.DEVNULL).decode().strip()


def derive_project_id() -> str:
    """Derive project slug from git remote or repo dirname."""
    try:
        remote = git("remote", "get-url", "origin")
        slug = remote.rstrip("/").removesuffix(".git").split("/")[-1].split(":")[-1]
        if slug:
            return slug
    except Exception:
        pass
    try:
        return os.path.basename(git("rev-parse", "--show-toplevel"))
    except Exception:
        return "default"


def get_commits(since: str | None, limit: int | None) -> list[dict]:
    """Return a list of commit dicts (hash, subject, body) from git log."""
    cmd = ["git", "log", "--no-merges", "--format=%H\x1f%s\x1f%b\x1e"]
    if since:
        cmd += [f"--since={since}"]
    if limit:
        cmd += [f"-{limit}"]
    raw = subprocess.check_output(cmd, stderr=subprocess.DEVNULL).decode()
    commits = []
    for record in raw.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        parts = record.split("\x1f", 2)
        if len(parts) < 2:
            continue
        hash_, subject = parts[0].strip(), parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        if not hash_ or not subject:
            continue
        commits.append({"hash": hash_, "subject": subject, "body": body})
    return commits


def get_changed_files(hash_: str) -> list[str]:
    """Return dotted module IDs for source files changed in a given commit."""
    try:
        out = git("diff-tree", "--no-commit-id", "-r", "--name-only", hash_)
        return [
            f.replace("/", ".").removesuffix(".py")
            for f in out.splitlines()
            if f.endswith((".py", ".ts", ".tsx"))
        ]
    except Exception:
        return []


# ── API call ───────────────────────────────────────────────────────────────────

def post_decision(payload: dict) -> dict:
    """POST a decision record to the Scopenos /api/decisions endpoint and return the response."""
    data = json.dumps(payload).encode()
    req = Request(
        f"{SCOPENOS_URL}/api/decisions",
        data=data,
        headers={"Content-Type": "application/json", "X-API-Key": SCOPENOS_API_KEY},
        method="POST",
    )
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def check_server() -> bool:
    """Verify the Scopenos server is reachable."""
    try:
        req = Request(f"{SCOPENOS_URL}/api/health", method="GET")
        with urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
        return resp.get("status") == "ok"
    except Exception as e:
        print(f"ERROR: cannot reach Scopenos at {SCOPENOS_URL}: {e}", file=sys.stderr)
        return False


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    """Parse CLI args and backfill git history as Scopenos decision records."""
    parser = argparse.ArgumentParser(description="Backfill Scopenos decisions from git history")
    parser.add_argument("--dry-run", action="store_true", help="Print decisions without writing")
    parser.add_argument("--since", metavar="DATE", help="Only commits after DATE (e.g. 2024-01-01)")
    parser.add_argument("--limit", type=int, metavar="N", help="Maximum number of commits to process")
    parser.add_argument("--scopenos-url", default=SCOPENOS_URL, help=f"Scopenos base URL (default: {SCOPENOS_URL})")
    parser.add_argument(
        "--project", metavar="ID",
        help="Project ID to tag decisions with (default: derived from git remote or dirname)",
    )
    parser.add_argument(
        "--repo-dir", metavar="PATH",
        help="Path to the git repository to backfill (default: current directory)",
    )
    args = parser.parse_args()

    if args.scopenos_url != SCOPENOS_URL:
        globals()["SCOPENOS_URL"] = args.scopenos_url

    if args.repo_dir:
        os.chdir(args.repo_dir)

    project_id = args.project or os.environ.get("SCOPENOS_PROJECT") or derive_project_id()
    print(f"Project ID: {project_id}")

    if not args.dry_run and not check_server():
        sys.exit(1)

    commits = get_commits(args.since, args.limit)
    print(f"Found {len(commits)} commits to process")
    if not commits:
        return

    ok = skipped = errors = 0

    for i, commit in enumerate(commits, 1):
        hash_  = commit["hash"]
        subject = commit["subject"]
        body   = commit["body"]
        type_  = classify(subject)
        description = subject + (" — " + body if body else "")
        linked = get_changed_files(hash_) or None

        payload = {
            "type": type_,
            "description": description,
            "trigger": f"git:{hash_[:8]}",
            "linked_function_ids": linked,
            "project_id": project_id,
        }

        prefix = f"[{i:3d}/{len(commits)}] {hash_[:8]} ({type_:14s})"

        if args.dry_run:
            print(f"{prefix} {subject[:60]}")
            ok += 1
            continue

        try:
            resp = post_decision(payload)
            if resp.get("status") == "ok":
                decision_id = resp.get("decision_id", "")[:8]
                print(f"{prefix} {subject[:50]} → {decision_id}")
                ok += 1
            else:
                print(f"{prefix} SKIP: {resp.get('detail', resp)}")
                skipped += 1
        except URLError as e:
            print(f"{prefix} ERROR: {e}")
            errors += 1
        except Exception as e:
            print(f"{prefix} ERROR: {e}")
            errors += 1

        time.sleep(DELAY_BETWEEN_CALLS)

    print(f"\nDone. ok={ok}  skipped={skipped}  errors={errors}")


if __name__ == "__main__":
    main()
