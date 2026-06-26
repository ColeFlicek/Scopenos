#!/usr/bin/env python3
"""
Backfill commit_function_changes from git history.

For each commit in a repo's git log, finds which Scopenos-indexed functions
live in the changed files and inserts (project_id, commit_hash, function_id)
rows. After running, get_co_change_functions() can surface empirical co-change
hints in get_impact_radius responses.

This is a one-time script per project. Re-running is safe (ON CONFLICT DO NOTHING).

Usage:
    DATABASE_URL=postgresql://scopenos:...@172.21.0.1/scopenos \\
        python3 scripts/backfill_cochange.py \\
        --project-id django \\
        --repo-path /tmp/scopenos-demos/django

    # Dry run — shows commit count and sample without writing
    python3 scripts/backfill_cochange.py \\
        --project-id django --repo-path /tmp/scopenos-demos/django --dry-run

    # Limit to recent history
    python3 scripts/backfill_cochange.py \\
        --project-id django --repo-path /tmp/scopenos-demos/django --limit 500
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone

try:
    import asyncpg
except ImportError:
    print("asyncpg not installed. Run: pip install asyncpg", file=sys.stderr)
    sys.exit(1)

_DEFAULT_DSN = "postgresql://scopenos:scopenos@localhost:5432/scopenos"
_BATCH_SIZE = 2000


# ── Git helpers ───────────────────────────────────────────────────────────────

def _git(*args: str, cwd: str) -> str:
    return subprocess.check_output(
        ["git"] + list(args), cwd=cwd, stderr=subprocess.DEVNULL
    ).decode().strip()


def get_commits(repo_path: str, limit: int | None, since: str | None) -> list[dict]:
    """Return [{hash, branch}] for every non-merge commit in the repo."""
    cmd = ["git", "log", "--no-merges", "--format=%H\x1f%D"]
    if since:
        cmd += [f"--since={since}"]
    if limit:
        cmd += [f"-{limit}"]
    raw = subprocess.check_output(cmd, cwd=repo_path, stderr=subprocess.DEVNULL).decode()
    commits = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\x1f", 1)
        hash_ = parts[0].strip()
        refs = parts[1].strip() if len(parts) > 1 else ""
        branch = _branch_from_refs(refs)
        if hash_:
            commits.append({"hash": hash_, "branch": branch})
    return commits


def _branch_from_refs(refs: str) -> str:
    """Extract a branch name from a git log --format=%D ref string."""
    for ref in refs.split(","):
        ref = ref.strip()
        if ref.startswith("HEAD -> "):
            return ref[8:]
        if ref and not ref.startswith("HEAD") and not ref.startswith("tag:"):
            return ref
    return ""


def get_changed_files(repo_path: str, commit_hash: str) -> list[str]:
    """Return list of file paths changed in a commit (relative to repo root)."""
    try:
        out = subprocess.check_output(
            ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", commit_hash],
            cwd=repo_path, stderr=subprocess.DEVNULL,
        ).decode().strip()
        return [f for f in out.splitlines() if f]
    except Exception:
        return []


# ── File → function mapping ───────────────────────────────────────────────────

def _build_suffix_map(file_map: dict[str, list[str]]) -> dict[str, list[str]]:
    """
    Build a suffix index so relative git paths can be matched against absolute
    node paths. Nodes are stored with absolute paths (/tmp/.../django/models/base.py)
    but git diff-tree returns relative paths (django/models/base.py).

    Keys are the last 1, 2, and 3 path components joined by '/'.
    First match wins — most specific (3 components) is tried first at lookup time.
    """
    suffix_map: dict[str, list[str]] = {}
    for abs_path, fn_ids in file_map.items():
        parts = abs_path.replace("\\", "/").split("/")
        for n in (3, 2, 1):
            if len(parts) >= n:
                key = "/".join(parts[-n:])
                suffix_map.setdefault(key, []).extend(fn_ids)
    return suffix_map


def _resolve_file(git_rel_path: str, suffix_map: dict[str, list[str]]) -> list[str]:
    """Map a git-relative file path to a list of function IDs using suffix matching."""
    parts = git_rel_path.replace("\\", "/").split("/")
    for n in (3, 2, 1):
        if len(parts) >= n:
            key = "/".join(parts[-n:])
            if key in suffix_map:
                return suffix_map[key]
    return []


# ── DB ────────────────────────────────────────────────────────────────────────

async def fetch_file_map(conn, project_id: str) -> dict[str, list[str]]:
    """Return {abs_file_path: [function_id, ...]} for all internal nodes."""
    rows = await conn.fetch(
        "SELECT id, file FROM nodes WHERE project_id = $1 AND is_external = 0",
        project_id,
    )
    mapping: dict[str, list[str]] = {}
    for row in rows:
        mapping.setdefault(row["file"], []).append(row["id"])
    return mapping


async def insert_batch(conn, rows: list[tuple]) -> int:
    if not rows:
        return 0
    await conn.executemany(
        """INSERT INTO commit_function_changes
               (project_id, commit_hash, function_id, branch, changed_at)
           VALUES ($1, $2, $3, $4, $5)
           ON CONFLICT (project_id, commit_hash, function_id) DO NOTHING""",
        rows,
    )
    return len(rows)


async def ensure_table(conn) -> None:
    """Create commit_function_changes and its indexes if they don't exist."""
    await conn.execute("""
        CREATE TABLE IF NOT EXISTS commit_function_changes (
            project_id   TEXT NOT NULL,
            commit_hash  TEXT NOT NULL,
            function_id  TEXT NOT NULL,
            branch       TEXT NOT NULL DEFAULT '',
            changed_at   TEXT NOT NULL,
            PRIMARY KEY (project_id, commit_hash, function_id)
        )
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cfc_project_function
        ON commit_function_changes(project_id, function_id)
    """)
    await conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_cfc_project_commit
        ON commit_function_changes(project_id, commit_hash)
    """)


async def show_summary(conn, project_id: str) -> None:
    """Print a cheap summary — avoids the O(n²) self-join over all pairs."""
    row = await conn.fetchrow(
        """SELECT COUNT(*) AS rows,
                  COUNT(DISTINCT commit_hash) AS commits,
                  COUNT(DISTINCT function_id) AS functions
           FROM commit_function_changes WHERE project_id = $1""",
        project_id,
    )
    print(f"  {row['rows']:,} rows — {row['commits']} commits — {row['functions']:,} functions indexed")
    # Show the most-committed function (appears in most commits) as a spot check
    top = await conn.fetch(
        """SELECT function_id, COUNT(DISTINCT commit_hash) AS n
           FROM commit_function_changes WHERE project_id = $1
           GROUP BY function_id ORDER BY n DESC LIMIT 5""",
        project_id,
    )
    if top:
        print("  Most-committed functions:")
        for r in top:
            print(f"    {r['n']:4d} commits  {r['function_id'].split('.')[-1]}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(
    dsn: str,
    project_id: str,
    repo_path: str,
    limit: int | None,
    since: str | None,
    dry_run: bool,
) -> None:
    t0 = datetime.now()

    print(f"Reading git log from {repo_path} ...")
    commits = get_commits(repo_path, limit, since)
    if not commits:
        print("No commits found.")
        return
    print(f"Found {len(commits)} commits")

    if dry_run:
        sample = commits[:5]
        for c in sample:
            files = get_changed_files(repo_path, c["hash"])
            print(f"  {c['hash'][:8]}  {len(files)} files changed  branch={c['branch'] or '?'}")
        print(f"\n[dry-run] Would process {len(commits)} commits. Re-run without --dry-run to write.")
        return

    conn = await asyncpg.connect(dsn)
    try:
        await ensure_table(conn)

        print(f"Loading file→function map for project '{project_id}' ...")
        file_map = await fetch_file_map(conn, project_id)
        if not file_map:
            print(
                f"ERROR: No nodes found for project_id='{project_id}'. "
                "Run index_project first, then re-run this script.",
                file=sys.stderr,
            )
            return

        fn_total = sum(len(v) for v in file_map.values())
        print(f"Loaded {len(file_map):,} files covering {fn_total:,} function IDs")

        suffix_map = _build_suffix_map(file_map)
        now = datetime.now(timezone.utc).isoformat()

        batch: list[tuple] = []
        total_rows = 0
        commits_with_matches = 0

        for i, commit in enumerate(commits, 1):
            hash_ = commit["hash"]
            branch = commit["branch"]
            files = get_changed_files(repo_path, hash_)

            fn_ids: set[str] = set()
            for f in files:
                fn_ids.update(_resolve_file(f, suffix_map))

            if fn_ids:
                commits_with_matches += 1
                for fn_id in fn_ids:
                    batch.append((project_id, hash_, fn_id, branch, now))

            if len(batch) >= _BATCH_SIZE:
                total_rows += await insert_batch(conn, batch)
                batch = []
                print(
                    f"  [{i:5d}/{len(commits)}] {commits_with_matches} matched  "
                    f"{total_rows:,} rows written ...",
                    end="\r",
                )

        if batch:
            total_rows += await insert_batch(conn, batch)

        elapsed = (datetime.now() - t0).total_seconds()
        print(f"\n\nDone in {elapsed:.1f}s")
        print(f"  Commits processed:          {len(commits):,}")
        print(f"  Commits with indexed files: {commits_with_matches:,}")
        print(f"  Rows written:               {total_rows:,}")

        print("\nSummary:")
        await show_summary(conn, project_id)

    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill commit_function_changes from git history"
    )
    parser.add_argument("--project-id", required=True,
                        help="Scopenos project_id (e.g. 'django')")
    parser.add_argument("--repo-path", required=True,
                        help="Absolute path to the git repo to mine")
    parser.add_argument("--dsn", default=None,
                        help="Postgres DSN (default: $DATABASE_URL or localhost)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Max commits to process (default: all)")
    parser.add_argument("--since", default=None,
                        help="Only commits after DATE (e.g. 2024-01-01)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be written without writing")
    args = parser.parse_args()

    dsn = args.dsn or os.getenv("DATABASE_URL", "")
    if not dsn:
        print(
            "ERROR: DATABASE_URL not set.\n"
            "Export it before running:\n"
            "  export DATABASE_URL=postgresql://scopenos:PASSWORD@HOST/scopenos",
            file=sys.stderr,
        )
        sys.exit(1)

    if not os.path.isdir(args.repo_path):
        print(f"ERROR: --repo-path '{args.repo_path}' does not exist", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(
        dsn=dsn,
        project_id=args.project_id,
        repo_path=args.repo_path,
        limit=args.limit,
        since=args.since,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()
