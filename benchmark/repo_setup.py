"""
Clone a repo at a specific commit, create a venv, and optionally index it
with Phronosis via the HTTP API (no direct Postgres access needed).

Clone strategy: one shared base clone per repo (full history), then
git worktree per task. Avoids re-downloading the full repo for every task.
"""
from __future__ import annotations

import glob
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

# indexed commits: base_commit → project_id (skip re-index if same commit appears twice)
_indexed_commits: dict[str, str] = {}

PHRONOSIS_URL = os.getenv("PHRONOSIS_URL", "http://100.71.88.106:3004")
PHRONOSIS_API_KEY = os.getenv("PHRONOSIS_API_KEY", "")

# Persistent base-clone location (survives across Python sessions via disk)
_BASE_CLONE_ROOT = Path(os.getenv("BENCH_CLONE_ROOT", "/tmp/phronosis-bench-base"))


@dataclass
class RepoContext:
    task: BenchmarkTask
    repo_path: str        # absolute path to the worktree for this task
    venv_python: str      # absolute path to venv python binary
    project_id: str       # Phronosis project_id (Path B only, else "")
    phronosis_indexed: bool


def setup_repo(
    task: BenchmarkTask,
    *,
    phronosis_index: bool = False,
    workdir: str | None = None,
    phronosis_dsn: str = "",  # unused — kept for compat
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
    parent = workdir or tempfile.mkdtemp(prefix="phronosis-bench-")
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
    if phronosis_index:
        project_id = _ensure_indexed(task, worktree_path)
        indexed = True

    return RepoContext(
        task=task,
        repo_path=worktree_path,
        venv_python=venv_python,
        project_id=project_id,
        phronosis_indexed=indexed,
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


def _create_venv(repo_path: str) -> str:
    """Create a venv inside the worktree and install the package (best-effort)."""
    venv_dir = os.path.join(repo_path, ".bench-venv")
    python = os.path.join(venv_dir, "bin", "python")

    if os.path.exists(python):
        return python  # already set up

    print(f"[setup] creating venv…")
    subprocess.run(["python3", "-m", "venv", venv_dir], check=True)
    subprocess.run(
        [python, "-m", "pip", "install", "--quiet", "-e", ".[dev,testing]"],
        cwd=repo_path, capture_output=True,
    )
    subprocess.run(
        [python, "-m", "pip", "install", "--quiet", "pytest"],
        cwd=repo_path, capture_output=True,
    )
    return python


def _ensure_indexed(task: BenchmarkTask, repo_path: str) -> str:
    """
    Index the repo with Phronosis via /api/index-bulk.
    Only sends src/**/*.py files — test files are not needed for call graph nav.
    Reuses the index if this commit was already indexed this session.
    """
    commit = task.base_commit
    if commit in _indexed_commits:
        print(f"[setup] reusing Phronosis index for {commit[:8]}")
        return _indexed_commits[commit]

    project_id = f"bench-{task.instance_id}"
    print(f"[setup] indexing → project '{project_id}'")

    # Prefer src/ layout (pytest, django, etc.) — avoids sending test files
    src_files = glob.glob(f"{repo_path}/src/**/*.py", recursive=True)
    if not src_files:
        src_files = [
            f for f in glob.glob(f"{repo_path}/**/*.py", recursive=True)
            if "/.bench-venv/" not in f and "/test" not in f.split(repo_path)[-1][:20]
        ]

    print(f"[setup] sending {len(src_files)} source files to Phronosis…")

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
            "project_root": repo_path,
            "project_id": project_id,
            "files": files,
        }).encode()

        req = urllib.request.Request(
            f"{PHRONOSIS_URL}/api/index-bulk",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": PHRONOSIS_API_KEY,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
                total_fns += result.get("functions_updated", 0)
        except Exception as exc:
            print(f"[setup] batch {i // batch_size + 1} failed: {exc}")

    print(f"[setup] indexed {total_fns} functions")
    _indexed_commits[commit] = project_id
    return project_id
