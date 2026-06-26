#!/usr/bin/env python3
"""
Index demo repos and mark them as available to all authenticated users.

Run from the project root on a machine where DATABASE_URL, OPENAI_API_KEY,
and ANTHROPIC_API_KEY are set (e.g. on TheHive with the K8s env exported):

    python scripts/index_demo_repos.py
    python scripts/index_demo_repos.py --skip-enrich   # index only, no LLM summaries
    python scripts/index_demo_repos.py --repos requests flask  # subset by slug

Scopenos itself is handled specially: it's already indexed, so the script
just marks it as a demo project without re-indexing.

Record your OpenAI and Anthropic dashboard totals BEFORE running, then again
AFTER, to compute per-repo cost for pricing projections.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# ── Demo repo manifest ─────────────────────────────────────────────────────────

@dataclass
class DemoRepo:
    slug: str
    display_name: str
    repo_url: str
    description: str
    already_indexed: bool = False  # skip clone+index, just mark as demo


DEMO_REPOS: list[DemoRepo] = [
    DemoRepo(
        slug="scopenos",
        display_name="Scopenos",
        repo_url="https://github.com/ColeFlicek/Scopenos",
        description="Scopenos itself — call graph, semantic embeddings, and decision memory for codebases.",
        already_indexed=True,
    ),
    DemoRepo(
        slug="requests",
        display_name="psf/requests",
        repo_url="https://github.com/psf/requests",
        description="The iconic Python HTTP library. Small, clean, and well-documented.",
        already_indexed=True,
    ),
    DemoRepo(
        slug="flask",
        display_name="pallets/flask",
        repo_url="https://github.com/pallets/flask",
        description="Lightweight Python web framework. Classic microframework design.",
        already_indexed=True,
    ),
    DemoRepo(
        slug="pytest",
        display_name="pytest-dev/pytest",
        repo_url="https://github.com/pytest-dev/pytest",
        description="Python testing framework. Medium-sized, plugin-heavy architecture.",
        already_indexed=True,
    ),
    DemoRepo(
        slug="django",
        display_name="django/django",
        repo_url="https://github.com/django/django",
        description="The web framework for perfectionists with deadlines. Largest SWE-bench repo.",
    ),
    DemoRepo(
        slug="sympy",
        display_name="sympy/sympy",
        repo_url="https://github.com/sympy/sympy",
        description="Python library for symbolic mathematics. Deep call graph across algebra, calculus, and more.",
    ),
    DemoRepo(
        slug="scikit-learn",
        display_name="scikit-learn/scikit-learn",
        repo_url="https://github.com/scikit-learn/scikit-learn",
        description="Machine learning in Python. Rich estimator hierarchy and pipeline architecture.",
    ),
    DemoRepo(
        slug="matplotlib",
        display_name="matplotlib/matplotlib",
        repo_url="https://github.com/matplotlib/matplotlib",
        description="Python 2D plotting library. Complex renderer and artist subsystem.",
    ),
    DemoRepo(
        slug="astropy",
        display_name="astropy/astropy",
        repo_url="https://github.com/astropy/astropy",
        description="Astronomy and astrophysics in Python. Deep domain model across units, coordinates, and I/O.",
    ),
    DemoRepo(
        slug="sphinx",
        display_name="sphinx-doc/sphinx",
        repo_url="https://github.com/sphinx-doc/sphinx",
        description="Python documentation generator. Extension-based architecture with rich builder hierarchy.",
    ),
    DemoRepo(
        slug="xarray",
        display_name="pydata/xarray",
        repo_url="https://github.com/pydata/xarray",
        description="N-dimensional labeled arrays in Python. NumPy/pandas-compatible data model.",
    ),
    DemoRepo(
        slug="pylint",
        display_name="pylint-dev/pylint",
        repo_url="https://github.com/pylint-dev/pylint",
        description="Python static analysis tool. Checker plugin architecture with AST traversal.",
    ),
    DemoRepo(
        slug="seaborn",
        display_name="mwaskom/seaborn",
        repo_url="https://github.com/mwaskom/seaborn",
        description="Statistical data visualization in Python. High-level matplotlib wrapper.",
    ),
]


# ── Services (same pattern as src/jobs.py) ────────────────────────────────────

def _dsn() -> str:
    return os.getenv("DATABASE_URL", "postgresql://scopenos:scopenos@localhost/scopenos")


async def _make_db():
    from src.call_graph.storage import CallGraphDB
    return await CallGraphDB.create(_dsn())


async def _make_services():
    from src.call_graph.storage import CallGraphDB
    from src.embeddings.embedder import EmbeddingStore
    from src.embeddings.pipeline import EmbeddingPipeline
    from src.indexer import Indexer

    db = await CallGraphDB.create(_dsn())
    embeddings = await EmbeddingStore.create(db)
    pipeline = EmbeddingPipeline(db, embeddings)
    indexer = Indexer(db, pipeline)
    return db, indexer, pipeline, embeddings


async def _mark_as_demo(db, repo: DemoRepo) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db._db.execute(
        """INSERT INTO demo_projects
               (project_id, display_name, description, repo_url, added_at, auto_update)
           VALUES ($1, $2, $3, $4, $5, 0)
           ON CONFLICT (project_id) DO UPDATE SET
               display_name = excluded.display_name,
               description  = excluded.description,
               repo_url     = excluded.repo_url""",
        (repo.slug, repo.display_name, repo.description, repo.repo_url, now),
    )
    await db._db.commit()


async def _process_repo(repo: DemoRepo, clone_dir: Path, skip_enrich: bool, yes: bool = False) -> dict:
    result = {"slug": repo.slug, "index": None, "enrich": None, "demo_marked": False}
    t0 = time.time()

    if repo.already_indexed:
        # Only needs DB — no embedding client required.
        db = await _make_db()
        try:
            projects = await db.list_projects()
            existing_ids = {p["id"] for p in projects}
            if repo.slug not in existing_ids and "ACIP" in existing_ids:
                print(f"  renaming ACIP → {repo.slug}")
                await db.rename_project("ACIP", repo.slug)
            elif repo.slug not in existing_ids:
                print(f"  WARNING: no indexed project found for {repo.slug}, skipping")
                return result
            else:
                print(f"  found existing index for {repo.slug}")
            await _mark_as_demo(db, repo)
            result["demo_marked"] = True
            print(f"  ✓ marked as demo in {time.time() - t0:.1f}s")
        finally:
            await db.close()
        return result

    # New repo — needs full services (DB + embeddings + indexer).
    db, indexer, pipeline, embeddings = await _make_services()
    try:
        repo_path = clone_dir / repo.slug
        if not repo_path.exists():
            print(f"  cloning {repo.repo_url} …")
            subprocess.run(
                ["git", "clone", "--depth=1", repo.repo_url, str(repo_path)],
                check=True, capture_output=True,
            )
        else:
            print(f"  {repo_path} exists, skipping clone")

        if not yes:
            from src.indexer import estimate_project
            est = estimate_project(str(repo_path))
            print(f"\n  Found ~{est['estimated_functions']:,} functions, "
                  f"~{est['estimated_classes']:,} classes in "
                  f"{est['files']:,} files ({est['lines']:,} lines).")
            print(f"  Estimated index time: {est['estimated_time']}")
            resp = input("  Continue? [y/N] ").strip().lower()
            if resp != "y":
                print(f"  Skipping {repo.slug}.")
                return result

        print(f"  indexing …")
        result["index"] = await indexer.index_project(str(repo_path), project_id=repo.slug)
        print(f"  indexed {result['index'].get('nodes_written', '?')} nodes in {time.time() - t0:.1f}s")

        t1 = time.time()
        print(f"  indexing schema objects (classes) …")
        from src.schema_objects import index_schema_objects as _index_schema
        result["schema"] = await _index_schema(db, embeddings, repo.slug, include_db_tables=False)
        schema_total = result["schema"].get("total", "?")
        print(f"  indexed {schema_total} schema objects in {time.time() - t1:.1f}s")

        if not skip_enrich:
            t1 = time.time()
            print(f"  enriching summaries …")
            result["enrich"] = await pipeline.enrich_summaries(repo.slug, limit=2000)
            enriched = result["enrich"].get("enriched", "?") if result["enrich"] else "?"
            print(f"  enriched {enriched} functions in {time.time() - t1:.1f}s")

        await _mark_as_demo(db, repo)
        result["demo_marked"] = True
        print(f"  ✓ done in {time.time() - t0:.1f}s total")

    finally:
        await db.close()

    return result


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--skip-enrich", action="store_true",
        help="Skip LLM summary generation (saves API cost, lower search quality)",
    )
    parser.add_argument(
        "--mark-only", action="store_true",
        help="Skip clone+index+enrich; only mark already-indexed repos as demo projects",
    )
    parser.add_argument(
        "--repos", nargs="+", metavar="SLUG",
        help=f"Index only these slugs. Available: {[r.slug for r in DEMO_REPOS]}",
    )
    parser.add_argument(
        "--clone-dir", default="/tmp/scopenos-demos",
        help="Directory to clone repos into (default: /tmp/scopenos-demos)",
    )
    parser.add_argument(
        "--pause", type=int, default=5,
        help="Seconds to pause between repos to avoid embedding API rate limits (default: 5)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the pre-index confirmation prompt and index immediately",
    )
    args = parser.parse_args()

    repos = DEMO_REPOS
    if args.repos:
        slugs = set(args.repos)
        repos = [r for r in DEMO_REPOS if r.slug in slugs]
        if not repos:
            print(f"No matching slugs. Available: {[r.slug for r in DEMO_REPOS]}")
            sys.exit(1)

    clone_dir = Path(args.clone_dir)
    clone_dir.mkdir(parents=True, exist_ok=True)

    print(f"\nScopenos demo indexer")
    print(f"Repos : {[r.slug for r in repos]}")
    print(f"Enrich: {'no (--skip-enrich)' if args.skip_enrich else 'yes'}")
    print(f"Clones: {clone_dir}")
    print(f"\nRecord API costs NOW before proceeding.\n")

    total_t0 = time.time()
    results = []

    for i, repo in enumerate(repos):
        print(f"── {repo.display_name} ──────────────────────────")
        skip = args.skip_enrich or args.mark_only
        if args.mark_only:
            # Override: treat as already_indexed so only demo marking runs
            repo = DemoRepo(
                slug=repo.slug,
                display_name=repo.display_name,
                repo_url=repo.repo_url,
                description=repo.description,
                already_indexed=True,
            )
        result = asyncio.run(_process_repo(repo, clone_dir, skip, yes=args.yes))
        results.append(result)
        print()
        if i < len(repos) - 1 and args.pause > 0:
            print(f"  (pausing {args.pause}s before next repo…)")
            time.sleep(args.pause)

    elapsed = time.time() - total_t0
    succeeded = [r for r in results if r["demo_marked"]]
    failed = [r for r in results if not r["demo_marked"]]

    print(f"{'─' * 48}")
    print(f"Finished in {elapsed:.1f}s")
    print(f"Succeeded : {[r['slug'] for r in succeeded]}")
    if failed:
        print(f"Failed    : {[r['slug'] for r in failed]}")

    print(f"\nRecord API costs NOW and subtract from before to get per-run cost.")
    print(f"Divide by {len(succeeded)} repos for average cost per repo.\n")

    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
