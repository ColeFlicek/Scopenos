#!/usr/bin/env python3
"""
Index Phronosis demo repos and register them in the demo_projects table.

Run once (or when adding new demo repos). Requires:
  - DATABASE_URL env var pointing at the target Postgres DB
  - OPENAI_API_KEY for embeddings
  - ANTHROPIC_API_KEY for enrich_summaries

Usage:
    python scripts/index_demo_repos.py [--dry-run] [--skip-enrich]

--dry-run:     Clone and check repos but don't index or register
--skip-enrich: Skip enrich_summaries (faster, lower quality)
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

DEMO_REPOS = [
    {
        "project_id": "requests",
        "display_name": "requests",
        "description": "The most popular Python HTTP library. Clean, focused, ~8K lines.",
        "repo_url": "https://github.com/psf/requests",
        "clone_url": "https://github.com/psf/requests.git",
    },
    {
        "project_id": "zod",
        "display_name": "zod",
        "description": "TypeScript-first schema validation with static type inference. ~15K lines.",
        "repo_url": "https://github.com/colinhacks/zod",
        "clone_url": "https://github.com/colinhacks/zod.git",
    },
    {
        "project_id": "pytest",
        "display_name": "pytest",
        "description": "The Python testing framework. Medium-sized, excellent docs coverage. ~40K lines.",
        "repo_url": "https://github.com/pytest-dev/pytest",
        "clone_url": "https://github.com/pytest-dev/pytest.git",
    },
]


@dataclass
class IndexResult:
    project_id: str
    functions_indexed: int
    functions_embedded: int
    functions_enriched: int
    elapsed_seconds: float
    error: str | None = None


async def index_one(
    repo: dict,
    clone_dir: str,
    dry_run: bool,
    skip_enrich: bool,
) -> IndexResult:
    from src.call_graph.storage import CallGraphDB
    from src.embeddings.embedder import EmbeddingStore
    from src.embeddings.pipeline import EmbeddingPipeline
    from src.indexer import Indexer

    pid = repo["project_id"]
    print(f"\n{'='*60}")
    print(f"  {pid}  ({repo['repo_url']})")
    print(f"{'='*60}")

    if dry_run:
        print(f"  [dry-run] would clone and index {pid}")
        return IndexResult(pid, 0, 0, 0, 0.0)

    t0 = time.monotonic()

    print(f"  Cloning…")
    repo_path = os.path.join(clone_dir, pid)
    subprocess.run(
        ["git", "clone", "--depth=1", "--quiet", repo["clone_url"], repo_path],
        check=True,
    )
    print(f"  Cloned to {repo_path}")

    dsn = os.getenv("DATABASE_URL", "postgresql://phronosis:phronosis@localhost/phronosis")
    db = await CallGraphDB.create(dsn)
    embeddings = await EmbeddingStore.create(db)
    pipeline = EmbeddingPipeline(db, embeddings)
    indexer = Indexer(db, pipeline)

    print(f"  Indexing call graph + embeddings…")
    result = await indexer.index_project(repo_path, project_id=pid)
    fns = result.get("functions_indexed", 0)
    embedded = result.get("functions_reembedded", 0)
    print(f"  Indexed {fns} functions, embedded {embedded}")

    enriched = 0
    if not skip_enrich and result.get("embedded_large_fallback", 0) > 0:
        print(f"  Running enrich_summaries ({result['embedded_large_fallback']} undocumented functions)…")
        batch = 0
        while True:
            enrich_result = await pipeline.enrich_summaries(pid, limit=200)
            done = enrich_result.get("enriched", 0)
            enriched += done
            batch += 1
            print(f"    batch {batch}: enriched {done}")
            if done == 0:
                break
        print(f"  Enriched {enriched} functions total")

    # Register in demo_projects
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    await db._db.execute(
        """INSERT INTO demo_projects (project_id, display_name, description, repo_url, last_indexed, auto_update, added_at)
           VALUES (?, ?, ?, ?, ?, 1, ?)
           ON CONFLICT(project_id) DO UPDATE SET
               display_name=excluded.display_name,
               description=excluded.description,
               repo_url=excluded.repo_url,
               last_indexed=excluded.last_indexed""",
        (pid, repo["display_name"], repo["description"], repo["repo_url"], now, now),
    )
    print(f"  Registered in demo_projects ✓")

    await db.close()
    elapsed = time.monotonic() - t0
    return IndexResult(pid, fns, embedded, enriched, elapsed)


async def main(dry_run: bool, skip_enrich: bool) -> None:
    print("Phronosis Demo Repo Indexer")
    print(f"Repos to index: {[r['project_id'] for r in DEMO_REPOS]}")
    if dry_run:
        print("DRY RUN — no data will be written or cloned")
    if skip_enrich:
        print("Skipping enrich_summaries (--skip-enrich)")
    print()

    with tempfile.TemporaryDirectory(prefix="phronosis-demo-") as clone_dir:
        results = []
        for repo in DEMO_REPOS:
            try:
                result = await index_one(repo, clone_dir, dry_run, skip_enrich)
            except Exception as exc:
                print(f"  ERROR: {exc}")
                result = IndexResult(repo["project_id"], 0, 0, 0, 0.0, str(exc))
            results.append(result)

    print(f"\n{'='*60}")
    print("  Summary")
    print(f"{'='*60}")
    total_fns = 0
    for r in results:
        status = f"✓ {r.functions_indexed} fns, {r.elapsed_seconds:.0f}s" if not r.error else f"✗ {r.error}"
        print(f"  {r.project_id:<15} {status}")
        total_fns += r.functions_indexed
    print(f"\n  Total functions indexed: {total_fns}")
    print(f"\n  Demo repos are now accessible to all authenticated users (read-only).")


def cli() -> None:
    parser = argparse.ArgumentParser(description="Index Phronosis demo repos")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-enrich", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.skip_enrich))


if __name__ == "__main__":
    cli()
