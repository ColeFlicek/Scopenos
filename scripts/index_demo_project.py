#!/usr/bin/env python3
"""
Index a demo repo into Scopenos via the HTTP API.

No DB credentials required — only SCOPENOS_URL and SCOPENOS_API_KEY.
The repo is cloned locally, Python files are batched and sent to /api/index-bulk.

Usage:
    export SCOPENOS_URL=http://100.71.88.106:3004
    export SCOPENOS_API_KEY=<your-key>

    # Index pytest
    python scripts/index_demo_project.py --repo pytest-dev/pytest --id pytest

    # Index django (large — ~1min)
    python scripts/index_demo_project.py --repo django/django --id django

    # Index Scopenos itself
    python scripts/index_demo_project.py --repo ColeFlicek/Scopenos --id scopenos

    # Skip clone if already on disk
    python scripts/index_demo_project.py --repo psf/requests --id requests --clone-to /tmp/demos

    # Index only (no enrich) — faster, less API cost
    python scripts/index_demo_project.py --repo pytest-dev/pytest --id pytest --no-enrich

Available demo repos (SWE-bench):
    pytest      pytest-dev/pytest
    django      django/django
    scikit-learn scikit-learn/scikit-learn
    matplotlib  matplotlib/matplotlib
    xarray      pydata/xarray
    sphinx      sphinx-doc/sphinx
    seaborn     mwaskom/seaborn
    pylint      pylint-dev/pylint

    # Non-SWE-bench demos
    requests    psf/requests
    flask       pallets/flask
    scopenos    ColeFlicek/Scopenos
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SCOPENOS_URL = os.getenv("SCOPENOS_URL", "http://100.71.88.106:3004")
SCOPENOS_API_KEY = os.getenv("SCOPENOS_API_KEY", "")

_CLONE_ROOT = Path(os.getenv("BENCH_CLONE_ROOT", "/tmp/scopenos-demos"))
_BATCH_SIZE = 50

# Files to exclude even if they match *.py
_EXCLUDE_PATTERNS = [
    "/.bench-venv/", "/.venv/", "/venv/", "/site-packages/",
    "/node_modules/", "/__pycache__/", "/build/", "/dist/",
    "/docs/", "/.eggs/", "/egg-info/", "/.tox/",
]


def _clone_or_update(repo: str, clone_path: Path) -> None:
    org, name = repo.split("/", 1)
    url = f"https://github.com/{org}/{name}.git"

    if clone_path.exists():
        print(f"[clone] {clone_path} already exists — fetching…")
        subprocess.run(["git", "fetch", "--quiet"], cwd=clone_path, capture_output=True)
    else:
        _CLONE_ROOT.mkdir(parents=True, exist_ok=True)
        print(f"[clone] cloning {repo} → {clone_path}")
        subprocess.run(["git", "clone", "--quiet", url, str(clone_path)], check=True)


def _collect_files(clone_path: Path, include_tests: bool = False) -> list[Path]:
    """Return all .py files excluding venv, build artifacts, and optionally tests."""
    files: list[Path] = []
    for p in clone_path.rglob("*.py"):
        rel = "/" + str(p.relative_to(clone_path)) + "/"
        if any(exc in rel for exc in _EXCLUDE_PATTERNS):
            continue
        if not include_tests and ("/test" in rel or "/tests/" in rel):
            continue
        files.append(p)
    return sorted(files)


def _index_files(files: list[Path], clone_path: Path, project_id: str) -> int:
    """Send files in batches to /api/index-bulk. Returns total functions indexed."""
    total_fns = 0
    batches = range(0, len(files), _BATCH_SIZE)
    for i, start in enumerate(batches):
        batch = files[start:start + _BATCH_SIZE]
        contents: dict[str, str] = {}
        for fp in batch:
            try:
                contents[str(fp)] = fp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                pass
        if not contents:
            continue

        payload = json.dumps({
            "project_root": str(clone_path),
            "project_id": project_id,
            "files": contents,
        }).encode()

        req = urllib.request.Request(
            f"{SCOPENOS_URL}/api/index-bulk",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-API-Key": SCOPENOS_API_KEY,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                result = json.loads(resp.read())
                batch_fns = result.get("functions_updated", 0)
                total_fns += batch_fns
                print(f"  batch {i + 1}/{len(batches)}: +{batch_fns} functions", end="\r")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except Exception:
                detail = body
            print(f"\n  batch {i + 1} failed: HTTP {exc.code} — {detail}")
        except Exception as exc:
            print(f"\n  batch {i + 1} failed: {exc}")

    print()
    return total_fns


def _enrich(project_id: str, limit: int = 2000) -> None:
    """Trigger LLM summary enrichment via /api/enrich (best-effort)."""
    payload = json.dumps({"limit": limit}).encode()
    req = urllib.request.Request(
        f"{SCOPENOS_URL}/api/enrich-summaries/{project_id}",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": SCOPENOS_API_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read())
            print(f"[enrich] queued job {result.get('job_id', '?')} (status: {result.get('status', '?')})")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            detail = json.loads(body).get("detail", body)
        except Exception:
            detail = body
        print(f"[enrich] skipped (non-fatal): HTTP {exc.code} — {detail}")
    except Exception as exc:
        print(f"[enrich] skipped (non-fatal): {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, help="GitHub org/name (e.g. pytest-dev/pytest)")
    parser.add_argument("--id", required=True, dest="project_id", help="Scopenos project_id slug")
    parser.add_argument("--clone-to", default=str(_CLONE_ROOT), help="Base directory for clones")
    parser.add_argument("--include-tests", action="store_true", help="Also index test files")
    parser.add_argument("--no-enrich", action="store_true", help="Skip LLM summary enrichment")
    args = parser.parse_args()

    if not SCOPENOS_API_KEY:
        print("ERROR: SCOPENOS_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    clone_root = Path(args.clone_to)
    slug = args.repo.replace("/", "__")
    clone_path = clone_root / slug

    t0 = time.time()

    _clone_or_update(args.repo, clone_path)
    files = _collect_files(clone_path, include_tests=args.include_tests)
    print(f"[index] {len(files)} Python files → project_id={args.project_id!r}")
    print(f"        ({SCOPENOS_URL})")

    total = _index_files(files, clone_path, args.project_id)
    print(f"[index] indexed {total:,} functions in {time.time() - t0:.1f}s")

    if not args.no_enrich:
        print("[enrich] starting LLM enrichment (this may take a few minutes)…")
        _enrich(args.project_id)

    print(f"\nDone. project_id={args.project_id!r} is ready in Scopenos.\n")
    print(f"Test it:  python -m benchmark.run check-mcp")
    print(f"          (or call get_project_home('{args.project_id}') from Claude)")


if __name__ == "__main__":
    main()
