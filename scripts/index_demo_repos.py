#!/usr/bin/env python3
"""
Index demo repos and mark them as available to all authenticated users.

Run from the project root on a machine where DATABASE_URL, OPENAI_API_KEY,
and ANTHROPIC_API_KEY are set (e.g. on TheHive with the K8s env exported):

    python scripts/index_demo_repos.py
    python scripts/index_demo_repos.py --skip-enrich   # index only, no LLM summaries
    python scripts/index_demo_repos.py --repos requests flask  # subset by slug

Phronosis itself is handled specially: it's already indexed, so the script
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
        slug="phronosis",
        display_name="Phronosis",
        repo_url="https://github.com/ColeFlicek/Phronosis",
        description="Phronosis itself — call graph, semantic embeddings, and decision memory for codebases.",
        already_indexed=True,
    ),
    DemoRepo(
        slug="requests",
        display_name="psf/requests",
        repo_url="https://github.com/psf/requests",
        description="The iconic Python HTTP library. Small, clean, and well-documented.",
    ),
    DemoRepo(
        slug="flask",
        display_name="pallets/flask",
        repo_url="https://github.com/pallets/flask",
        description="Lightweight Python web framework. Classic microframework design.",
    ),
    DemoRepo(
        slug="pytest",
        display_name="pytest-dev/pytest",
        repo_url="https://github.com/pytest-dev/pytest",
        description="Python testing framework. Medium-sized, plugin-heavy architecture.",
    ),
    DemoRepo(
        slug="gin",
        display_name="gin-gonic/gin",
        repo_url="https://github.com/gin-gonic/gin",
        description="High-performance Go HTTP framework. Demonstrates multi-language indexing.",
    ),
]


# ── Services (same pattern as src/jobs.py) ────────────────────────────────────

def _dsn() -> str:
    return os.getenv("DATABASE_URL", "postgresql://phronosis:phronosis@localhost/phronosis")


async def _make_services():
    from src.call_graph.storage import CallGraphDB
    from src.embeddings.embedder import EmbeddingStore
    from src.embeddings.pipeline import EmbeddingPipeline
    from src.indexer import Indexer

    db = await CallGraphDB.create(_dsn())
    embeddings = await EmbeddingStore.create(db)
    pipeline = EmbeddingPipeline(db, embeddings)
    indexer = Indexer(db, pipeline)
    return db, indexer, pipeline


async def _mark_as_demo(db, repo: DemoRepo) -> None:
    now = datetime.now(timezone.utc).isoformat()
    await db._db.execute(
        """INSERT INTO demo_projects
               (project_id, display_name, description, repo_url, added_at, auto_update)
           VALUES (?, ?, ?, ?, ?, 0)
           ON CONFLICT (project_id) DO UPDATE SET
               display_name = excluded.display_name,
               description  = excluded.description,
               repo_url     = excluded.repo_url""",
        (repo.slug, repo.display_name, repo.description, repo.repo_url, now),
    )
    await db._db.commit()


async def _process_repo(repo: DemoRepo, clone_dir: Path, skip_enrich: bool) -> dict:
    db, indexer, pipeline = await _make_services()
    result = {"slug": repo.slug, "index": None, "enrich": None, "demo_marked": False}
    t0 = time.time()

    try:
        if repo.already_indexed:
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
        else:
            repo_path = clone_dir / repo.slug
            if not repo_path.exists():
                print(f"  cloning {repo.repo_url} …")
                subprocess.run(
                    ["git", "clone", "--depth=1", repo.repo_url, str(repo_path)],
                    check=True, capture_output=True,
                )
            else:
                print(f"  {repo_path} exists, skipping clone")

            print(f"  indexing …")
            result["index"] = await indexer.index_project(str(repo_path), project_id=repo.slug)
            print(f"  indexed {result['index'].get('nodes_written', '?')} nodes in {time.time() - t0:.1f}s")

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
        "--repos", nargs="+", metavar="SLUG",
        help=f"Index only these slugs. Available: {[r.slug for r in DEMO_REPOS]}",
    )
    parser.add_argument(
        "--clone-dir", default="/tmp/phronosis-demos",
        help="Directory to clone repos into (default: /tmp/phronosis-demos)",
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

    print(f"\nPhronosis demo indexer")
    print(f"Repos : {[r.slug for r in repos]}")
    print(f"Enrich: {'no (--skip-enrich)' if args.skip_enrich else 'yes'}")
    print(f"Clones: {clone_dir}")
    print(f"\nRecord API costs NOW before proceeding.\n")

    total_t0 = time.time()
    results = []

    for repo in repos:
        print(f"── {repo.display_name} ──────────────────────────")
        result = asyncio.run(_process_repo(repo, clone_dir, args.skip_enrich))
        results.append(result)
        print()

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
