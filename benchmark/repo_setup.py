"""
Clone a repo at a specific commit, create a venv, and optionally index it
with Scopenos via the HTTP API (no direct Postgres access needed).

Clone strategy: one shared base clone per repo (full history), then
git worktree per task. Avoids re-downloading the full repo for every task.
"""
from __future__ import annotations

import asyncio
import glob
import hashlib
import json
import os
import subprocess
import tempfile
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .loader import BenchmarkTask

# base_clone_dir: shared full clone, keyed by repo slug
_base_clones: dict[str, str] = {}

# In-session cache: base_commit → fork_project_id
# Avoids duplicate HTTP calls within a single run.
_indexed_commits: dict[str, str] = {}

# In-session cache: repo slug → True when base project is confirmed to exist in org_benchmark
_base_indexed: set[str] = set()

# Projects pre-seeded into org_benchmark by copying from org_demos.
# These already exist — _ensure_base_project must not overwrite them.
_PRESEEDED = {"pytest", "django", "flask", "requests"}

SCOPENOS_URL = os.getenv("SCOPENOS_URL", "http://100.71.88.106:3004")
SCOPENOS_API_KEY = os.getenv("SCOPENOS_API_KEY", "")
# Separate key for benchmark indexing → routes to org_benchmark, not production.
# Falls back to SCOPENOS_API_KEY so local dev works without a second key.
BENCH_API_KEY = os.getenv("BENCH_API_KEY", "") or SCOPENOS_API_KEY

# Persistent base-clone location (survives across Python sessions via disk)
_BASE_CLONE_ROOT = Path(os.getenv("BENCH_CLONE_ROOT", "/tmp/scopenos-bench-base"))

# Disk cache: set of base project IDs already confirmed in org_benchmark.
# Persists across sessions so the full base-clone index only runs once per repo.
_INDEXED_CACHE_FILE = _BASE_CLONE_ROOT / "bench-indexed.json"


def _load_indexed_cache() -> set[str]:
    try:
        return set(json.loads(_INDEXED_CACHE_FILE.read_text()))
    except Exception:
        return set()


def _save_indexed_cache(cache: set[str]) -> None:
    try:
        _BASE_CLONE_ROOT.mkdir(parents=True, exist_ok=True)
        _INDEXED_CACHE_FILE.write_text(json.dumps(sorted(cache)))
    except Exception:
        pass


_disk_indexed: set[str] = _load_indexed_cache()


@dataclass
class RepoContext:
    task: BenchmarkTask
    repo_path: str        # absolute path to the worktree for this task
    venv_python: str      # absolute path to venv python binary
    project_id: str       # Scopenos project_id (Path B only, else "")
    scopenos_indexed: bool


def setup_repo(
    task: BenchmarkTask,
    *,
    scopenos_index: bool = False,
    workdir: str | None = None,
    scopenos_dsn: str = "",
) -> RepoContext:
    """
    Set up an isolated working directory for a benchmark task.

    Uses a shared base clone + git worktree so the repo is only downloaded
    once per run, regardless of how many tasks target the same repo.
    """
    org, name = task.repo.split("/")
    slug = f"{org}__{name}"

    base_clone = _ensure_base_clone(task.repo, slug)

    # Worktree path — one per task, in the workdir or a temp dir
    parent = workdir or tempfile.mkdtemp(prefix="scopenos-bench-")
    worktree_path = os.path.join(parent, task.instance_id)

    if not os.path.exists(worktree_path):
        print(f"[setup] worktree {task.base_commit[:8]} → {worktree_path}")
        subprocess.run(
            ["git", "worktree", "add", "--detach", worktree_path, task.base_commit],
            cwd=base_clone,
            check=True,
            capture_output=True,
        )
    else:
        print(f"[setup] reusing existing worktree {worktree_path}")

    venv_python = _create_venv(worktree_path)

    project_id = ""
    indexed = False
    if scopenos_index:
        project_id = _ensure_indexed(task, worktree_path, base_clone, dsn=scopenos_dsn)
        indexed = True

    return RepoContext(
        task=task,
        repo_path=worktree_path,
        venv_python=venv_python,
        project_id=project_id,
        scopenos_indexed=indexed,
    )


def cleanup_repo(ctx: RepoContext) -> None:
    """Remove the worktree (not the base clone)."""
    if os.path.exists(ctx.repo_path):
        # Find the base clone to remove the worktree registration
        parent = str(Path(ctx.repo_path).parent)
        slug = ctx.task.repo.replace("/", "__")
        base = _base_clones.get(slug) or str(_BASE_CLONE_ROOT / slug)
        try:
            subprocess.run(
                ["git", "worktree", "remove", "--force", ctx.repo_path],
                cwd=base,
                capture_output=True,
            )
        except Exception:
            import shutil
            shutil.rmtree(ctx.repo_path, ignore_errors=True)
        print(f"[setup] removed worktree {ctx.repo_path}")


def _ensure_base_clone(repo: str, slug: str) -> str:
    """
    Ensure a full clone of the repo exists at _BASE_CLONE_ROOT/slug.
    If it already exists (from a previous run), just fetch to update.
    Returns the path to the base clone.
    """
    if slug in _base_clones:
        return _base_clones[slug]

    _BASE_CLONE_ROOT.mkdir(parents=True, exist_ok=True)
    clone_path = str(_BASE_CLONE_ROOT / slug)

    if os.path.exists(clone_path):
        print(f"[setup] base clone exists at {clone_path}, fetching…")
        subprocess.run(["git", "fetch", "--quiet"], cwd=clone_path, capture_output=True)
    else:
        org, name = repo.split("/")
        clone_url = f"https://github.com/{org}/{name}.git"
        print(f"[setup] cloning {repo} → {clone_path} (full history, once per run)")
        subprocess.run(
            ["git", "clone", "--quiet", clone_url, clone_path],
            check=True,
        )

    _base_clones[slug] = clone_path
    return clone_path


def _pick_bench_python() -> str:
    if p := os.getenv("BENCH_PYTHON"):
        return p
    # Prefer Python 3.9 (SWE-bench Lite tasks target Python 3.8/3.9)
    for candidate in [
        "/opt/miniforge/envs/py39/bin/python",
        "/opt/miniforge/envs/py311/bin/python",
        "python3.9",
        "python3.10",
        "python3.11",
        "python3",
    ]:
        if candidate.startswith("/") and os.path.exists(candidate):
            return candidate
        elif not candidate.startswith("/"):
            import shutil
            if shutil.which(candidate):
                return candidate
    return "python3"


_BENCH_PYTHON = _pick_bench_python()


def _create_venv(repo_path: str) -> str:
    """Create a venv inside the worktree and install the package (best-effort)."""
    venv_dir = os.path.join(repo_path, ".bench-venv")
    python = os.path.join(venv_dir, "bin", "python")

    if os.path.exists(python):
        # Verify the existing venv has pytest — if not, pip-bootstrap it
        check = subprocess.run([python, "-m", "pytest", "--version"], capture_output=True)
        if check.returncode == 0:
            return python
        # Venv exists but pytest is missing (created before pip was available) — add it
        subprocess.run([python, "-m", "ensurepip", "--upgrade"], capture_output=True)
        subprocess.run([python, "-m", "pip", "install", "--quiet", "pytest"], cwd=repo_path, capture_output=True)
        return python

    is_django = os.path.exists(os.path.join(repo_path, "tests", "runtests.py"))

    print(f"[setup] creating venv with {_BENCH_PYTHON}…")
    subprocess.run([_BENCH_PYTHON, "-m", "venv", venv_dir], check=True)

    if is_django:
        # Django: install the package itself + test deps
        subprocess.run(
            [python, "-m", "pip", "install", "--quiet", "-e", "."],
            cwd=repo_path, capture_output=True,
        )
        subprocess.run(
            [python, "-m", "pip", "install", "--quiet",
             "pytest", "pytest-django", "asgiref", "sqlparse", "pytz"],
            cwd=repo_path, capture_output=True,
        )
    else:
        subprocess.run(
            [python, "-m", "pip", "install", "--quiet", "-e", ".[dev,testing]"],
            cwd=repo_path, capture_output=True,
        )
        subprocess.run(
            [python, "-m", "pip", "install", "--quiet", "pytest"],
            cwd=repo_path, capture_output=True,
        )
        # Older SWE-bench repos (pre-2022) used hypothesis hooks not compatible
        # with hypothesis>=6 (which added 'collection_path' to pytest_ignore_collect).
        subprocess.run(
            [python, "-m", "pip", "install", "--quiet", "hypothesis<6"],
            cwd=repo_path, capture_output=True,
        )
    return python


def _ensure_indexed(task: BenchmarkTask, repo_path: str, base_clone: str, *, dsn: str = "") -> str:
    """
    Ensure a Scopenos fork project exists for this task's base_commit.

    Strategy:
      1. One base project per repo slug (bench-{slug}), indexed once from the
         base clone at HEAD and written to org_benchmark via BENCH_API_KEY.
      2. One fork per unique base_commit (bench-{slug}-{sha8}), created via
         POST /api/fork-from-files. The benchmark runner (this process) computes
         the git diff + file contents and sends them over HTTP to the MCP server
         at SCOPENOS_URL — no server-side git access required.

    Both the base project and each fork persist in org_benchmark across sessions.
    In-session caches (_base_indexed, _indexed_commits) avoid redundant HTTP calls.
    """
    commit = task.base_commit
    if commit in _indexed_commits:
        print(f"[setup] reusing fork for {commit[:8]}")
        return _indexed_commits[commit]

    org, name = task.repo.split("/")
    slug = f"{org}__{name}"
    # Base project ID matches the project name in org_benchmark (copied from org_demos).
    base_project_id = name
    fork_project_id = f"{name}-fork-{commit[:8]}"

    # ── Step 1: ensure base project exists in org_benchmark ───────────────────
    if slug not in _base_indexed and base_project_id not in _disk_indexed and base_project_id not in _PRESEEDED:
        _ensure_base_project(base_project_id, base_clone)
        _disk_indexed.add(base_project_id)
        _save_indexed_cache(_disk_indexed)
    _base_indexed.add(slug)

    # ── Step 2: create fork at base_commit via /api/fork-from-files ───────────
    print(f"[setup] forking {base_project_id} → {fork_project_id} at {commit[:8]}")

    changed_rel = _git_changed_files(base_clone, commit)
    if changed_rel:
        files = _git_show_files(base_clone, commit, changed_rel)
    else:
        files = {}

    payload = json.dumps({
        "parent_project_id": base_project_id,
        "fork_project_id": fork_project_id,
        "files": files,
        "project_root": base_clone,
        "target_commit": commit,
    }).encode()

    req = urllib.request.Request(
        f"{SCOPENOS_URL}/api/fork-from-files",
        data=payload,
        headers={"Content-Type": "application/json", "X-API-Key": BENCH_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read())
            delta = result.get("delta", {})
            if delta.get("already_exists"):
                print(f"[setup] fork {fork_project_id} already exists — reusing")
            else:
                print(f"[setup] fork created: {delta}")
    except Exception as exc:
        print(f"[setup] fork-from-files failed: {exc}")

    _indexed_commits[commit] = fork_project_id

    if dsn:
        source_project_id = task.repo.split("/")[-1]
        _seed_cochange(
            dsn=dsn,
            bench_project_id=fork_project_id,
            source_project_id=source_project_id,
            base_commit=commit,
            base_clone_path=base_clone,
            worktree_path=repo_path,
        )

    return fork_project_id


def _ensure_base_project(base_project_id: str, base_clone: str) -> None:
    """Index the base clone (at HEAD) into org_benchmark as the base project.

    Idempotent: if the project already exists, index_changes upserts with no
    net effect. Only runs once per repo slug per session (_base_indexed guard).
    """
    src_files = glob.glob(f"{base_clone}/src/**/*.py", recursive=True)
    if not src_files:
        src_files = [
            f for f in glob.glob(f"{base_clone}/**/*.py", recursive=True)
            if "/.bench-venv/" not in f and "/test" not in f.split(base_clone)[-1][:20]
        ]

    print(f"[setup] indexing base project '{base_project_id}' ({len(src_files)} files)…")

    batch_size = 50
    total_fns = 0
    for i in range(0, len(src_files), batch_size):
        batch = src_files[i:i + batch_size]
        files: dict[str, str] = {}
        for fp in batch:
            try:
                files[fp] = Path(fp).read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if not files:
            continue

        payload = json.dumps({
            "project_root": base_clone,
            "project_id": base_project_id,
            "files": files,
        }).encode()

        req = urllib.request.Request(
            f"{SCOPENOS_URL}/api/index-bulk",
            data=payload,
            headers={"Content-Type": "application/json", "X-API-Key": BENCH_API_KEY},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
                total_fns += result.get("functions_updated", 0)
        except Exception as exc:
            print(f"[setup] base index batch {i // batch_size + 1} failed: {exc}")

    print(f"[setup] base project ready: {total_fns} functions")


def _git_changed_files(repo_path: str, target_commit: str) -> list[str]:
    """Return .py/.ts/.js paths that differ between target_commit and HEAD."""
    _SUPPORTED = {".py", ".ts", ".tsx", ".js", ".jsx"}
    try:
        out = subprocess.check_output(
            ["git", "-C", repo_path, "diff", "--name-only", target_commit, "HEAD"],
            stderr=subprocess.DEVNULL,
            text=True,
        )
        return [
            line.strip() for line in out.splitlines()
            if line.strip() and Path(line.strip()).suffix.lower() in _SUPPORTED
        ]
    except subprocess.CalledProcessError:
        return []


def _git_show_files(repo_path: str, commit: str, rel_paths: list[str]) -> dict[str, str]:
    """Return {abs_file_path: content_at_commit} for each rel_path.

    Uses the same absolute path format as the base project index so the server
    can match nodes by file path when applying the fork delta.
    """
    result: dict[str, str] = {}
    for rel in rel_paths:
        try:
            content = subprocess.check_output(
                ["git", "-C", repo_path, "show", f"{commit}:{rel}"],
                stderr=subprocess.DEVNULL,
                text=True,
            )
            result[str(Path(repo_path) / rel)] = content
        except subprocess.CalledProcessError:
            pass  # file didn't exist at this commit — skip
    return result


# ── Co-change seeding ─────────────────────────────────────────────────────────

def _file_hash_at_commit(repo_path: str, commit: str, rel_path: str) -> str | None:
    """SHA256 of a file at a specific git commit. None if path doesn't exist at that commit."""
    try:
        content = subprocess.check_output(
            ["git", "show", f"{commit}:{rel_path}"],
            cwd=repo_path,
            stderr=subprocess.DEVNULL,
        )
        return hashlib.sha256(content).hexdigest()
    except Exception:
        return None


def _file_hash_on_disk(abs_path: str) -> str | None:
    """SHA256 of a file on disk. None if unreadable."""
    try:
        return hashlib.sha256(Path(abs_path).read_bytes()).hexdigest()
    except Exception:
        return None


def _seed_cochange(
    *,
    dsn: str,
    bench_project_id: str,
    source_project_id: str,
    base_commit: str,
    base_clone_path: str,
    worktree_path: str,
    min_count: int = 3,
) -> None:
    """
    Copy co_change history from source_project into bench_project, gated by:

    1. Ancestor filter — only commits reachable from base_commit (no future data).
    2. Hash check — for each function in a co_change row, the file on disk at
       base_commit must be byte-identical to the file at the most recent co_change
       commit. If the hashes differ the function was refactored since it last
       co_changed, and its historical coupling signal is stale.
    """
    try:
        asyncio.run(_seed_cochange_async(
            dsn=dsn,
            bench_project_id=bench_project_id,
            source_project_id=source_project_id,
            base_commit=base_commit,
            base_clone_path=base_clone_path,
            worktree_path=worktree_path,
            min_count=min_count,
        ))
    except Exception as exc:
        print(f"[cochange] seed failed (non-fatal): {exc}")


async def _seed_cochange_async(
    *,
    dsn: str,
    bench_project_id: str,
    source_project_id: str,
    base_commit: str,
    base_clone_path: str,
    worktree_path: str,
    min_count: int,
) -> None:
    try:
        import asyncpg
    except ImportError:
        print("[cochange] asyncpg not available — skipping co_change seed")
        return

    # ── Step 1: ancestor commits in reverse-chronological order (newest first) ──
    raw = subprocess.check_output(
        ["git", "log", "--format=%H", base_commit],
        cwd=base_clone_path, stderr=subprocess.DEVNULL,
    ).decode()
    ancestors: list[str] = [h for h in raw.splitlines() if h.strip()]
    ancestor_set = set(ancestors)
    if not ancestors:
        print(f"[cochange] no ancestors found for {base_commit[:8]}")
        return

    conn = await asyncpg.connect(dsn)
    try:
        # ── Step 2: bench project function IDs → absolute file paths ──────────
        bench_rows = await conn.fetch(
            "SELECT id, file FROM nodes WHERE project_id = $1 AND is_external = 0",
            bench_project_id,
        )
        bench_fn_file: dict[str, str] = {r["id"]: r["file"] for r in bench_rows}
        if not bench_fn_file:
            print(f"[cochange] {bench_project_id} has no indexed functions — skipping")
            return

        # ── Step 3: pull co_change rows for source project × bench functions ──
        src_rows = await conn.fetch(
            """SELECT commit_hash, function_id
               FROM commit_function_changes
               WHERE project_id = $1 AND function_id = ANY($2)""",
            source_project_id, list(bench_fn_file.keys()),
        )
        # Keep only ancestor commits; build fn_id → set(commits)
        fn_commits: dict[str, set[str]] = {}
        for r in src_rows:
            if r["commit_hash"] in ancestor_set:
                fn_commits.setdefault(r["function_id"], set()).add(r["commit_hash"])

        if not fn_commits:
            print(
                f"[cochange] no history for {source_project_id} functions "
                f"in ancestors of {base_commit[:8]} — backfill may not cover this repo"
            )
            return

        # ── Step 4: find the most-recent co_change commit per function ─────────
        # ancestors is newest-first; first hit is the latest co_change commit.
        # Build a reverse index for O(1) lookups inside the loop.
        commit_to_fns: dict[str, set[str]] = {}
        for fn_id, commits in fn_commits.items():
            for c in commits:
                commit_to_fns.setdefault(c, set()).add(fn_id)

        fn_last_commit: dict[str, str] = {}
        remaining = set(fn_commits.keys())
        for ancestor in ancestors:
            if not remaining:
                break
            for fn_id in commit_to_fns.get(ancestor, set()) & remaining:
                fn_last_commit[fn_id] = ancestor
                remaining.discard(fn_id)

        # ── Step 5: hash check — skip functions refactored since last co_change ─
        # Also enforce min_count: functions that co-changed fewer times than the
        # threshold are likely coincidental and carry no reliable coupling signal.
        valid_fns: set[str] = set()
        stale_count = 0
        missing_count = 0

        for fn_id, last_commit in fn_last_commit.items():
            if len(fn_commits.get(fn_id, set())) < min_count:
                missing_count += 1
                continue
            abs_file = bench_fn_file.get(fn_id)
            if not abs_file:
                missing_count += 1
                continue

            rel_path = os.path.relpath(abs_file, worktree_path)

            hash_now = _file_hash_on_disk(abs_file)
            hash_then = _file_hash_at_commit(base_clone_path, last_commit, rel_path)

            if hash_now is None or hash_then is None:
                missing_count += 1
                continue

            if hash_now == hash_then:
                valid_fns.add(fn_id)
            else:
                stale_count += 1

        print(
            f"[cochange] hash check: {len(valid_fns)} valid, "
            f"{stale_count} stale (refactored since last co_change), "
            f"{missing_count} unresolvable"
        )

        if not valid_fns:
            return

        # ── Step 6: copy rows for valid functions only ─────────────────────────
        to_copy = [
            (bench_project_id, r["commit_hash"], r["function_id"], "", "")
            for r in src_rows
            if r["function_id"] in valid_fns and r["commit_hash"] in ancestor_set
        ]

        if to_copy:
            await conn.executemany(
                """INSERT INTO commit_function_changes
                       (project_id, commit_hash, function_id, branch, changed_at)
                   VALUES ($1, $2, $3, $4, $5)
                   ON CONFLICT (project_id, commit_hash, function_id) DO NOTHING""",
                to_copy,
            )
            print(f"[cochange] seeded {len(to_copy):,} rows into {bench_project_id}")

    finally:
        await conn.close()
