#!/usr/bin/env python3
"""
Push git co-change history to the Scopenos /api/backfill-cochange endpoint.

The server has no git access to the local clone, so this script reads the
commit history and changed-file lists locally, then sends them over HTTP.
The server resolves file paths → function IDs and writes commit_function_changes.

Usage:
    BENCH_API_KEY=scopenos-bench-... \\
        python3 scripts/push_cochange_history.py \\
        --project-id django \\
        --repo-path /tmp/scopenos-bench-base/django__django \\
        --limit 2000

    # Dry run — shows commit count without sending
    python3 scripts/push_cochange_history.py ... --dry-run
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

SCOPENOS_URL = os.getenv("SCOPENOS_URL", "http://100.71.88.106:3004")
BENCH_API_KEY = os.getenv("BENCH_API_KEY", "") or os.getenv("SCOPENOS_API_KEY", "")

_SUPPORTED = {".py", ".ts", ".tsx", ".js", ".jsx"}
_BATCH_SIZE = 200  # commits per HTTP call


def git(*args: str, cwd: str) -> str:
    return subprocess.check_output(["git"] + list(args), cwd=cwd,
                                   stderr=subprocess.DEVNULL, text=True).strip()


def get_commits(repo_path: str, limit: int | None, since: str | None) -> list[str]:
    cmd = ["log", "--format=%H"]
    if since:
        cmd += [f"--since={since}"]
    if limit:
        cmd += [f"-{limit}"]
    out = git(*cmd, cwd=repo_path)
    return [h for h in out.splitlines() if h.strip()]


def get_changed_files(repo_path: str, commit_hash: str) -> list[str]:
    try:
        out = git("diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash, cwd=repo_path)
        return [
            f for f in out.splitlines()
            if f.strip() and Path(f.strip()).suffix.lower() in _SUPPORTED
        ]
    except subprocess.CalledProcessError:
        return []


def send_batch(commits: list[dict], project_id: str, project_root: str) -> dict:
    payload = json.dumps({
        "project_id": project_id,
        "project_root": project_root,
        "commits": commits,
    }).encode()
    req = urllib.request.Request(
        f"{SCOPENOS_URL}/api/backfill-cochange",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": BENCH_API_KEY,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--repo-path", required=True)
    parser.add_argument("--limit", type=int, default=2000)
    parser.add_argument("--since", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not BENCH_API_KEY and not args.dry_run:
        print("ERROR: set BENCH_API_KEY env var", file=sys.stderr)
        sys.exit(1)

    repo = args.repo_path
    print(f"Reading git history from {repo} (limit={args.limit})...")
    commits = get_commits(repo, args.limit, args.since)
    print(f"Found {len(commits)} commits")

    if args.dry_run:
        sample = commits[:5]
        for h in sample:
            files = get_changed_files(repo, h)
            print(f"  {h[:8]}: {len(files)} .py/.ts files")
        print(f"  ... and {len(commits) - 5} more")
        return

    # Collect commit data
    batch: list[dict] = []
    total_inserted = 0
    total_processed = 0
    total_skipped = 0

    for i, commit_hash in enumerate(commits):
        files = get_changed_files(repo, commit_hash)
        if files:
            batch.append({"hash": commit_hash, "files": files})

        if len(batch) >= _BATCH_SIZE or (i == len(commits) - 1 and batch):
            result = send_batch(batch, args.project_id, repo)
            total_inserted += result.get("inserted", 0)
            total_processed += result.get("commits_processed", 0)
            total_skipped += result.get("commits_skipped", 0)
            print(f"  [{i+1}/{len(commits)}] batch done: "
                  f"+{result.get('inserted', 0)} rows "
                  f"({result.get('commits_processed', 0)} commits)")
            batch = []

    print(f"\nDone: {total_inserted} rows inserted, "
          f"{total_processed} commits with signal, "
          f"{total_skipped} skipped (no matching functions)")


if __name__ == "__main__":
    main()
