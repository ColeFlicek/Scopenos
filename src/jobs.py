"""
RQ job functions for long-running Phronosis operations.

Each function is a synchronous wrapper around async indexing code.
Workers run these in a separate process with their own DB pool.
All functions must be importable at module level (RQ serialises them by name).
"""
from __future__ import annotations

import asyncio
import os


def _dsn() -> str:
    return os.getenv("DATABASE_URL", "postgresql://phronosis:phronosis@localhost/phronosis")


async def _make_indexer():
    from .call_graph.storage import CallGraphDB
    from .embeddings.embedder import EmbeddingStore
    from .embeddings.pipeline import EmbeddingPipeline
    from .indexer import Indexer
    db = await CallGraphDB.create(_dsn())
    embeddings = await EmbeddingStore.create(db)
    pipeline = EmbeddingPipeline(db, embeddings)
    return db, Indexer(db, pipeline), pipeline


def run_index_project(path: str, project_id: str) -> dict:
    """RQ job: full project index."""
    async def _run():
        db, indexer, _ = await _make_indexer()
        try:
            return await indexer.index_project(path, project_id=project_id)
        finally:
            await db.close()
    return asyncio.run(_run())


def run_enrich_summaries(project_id: str, limit: int = 500, force: bool = False) -> dict:
    """RQ job: generate LLM summaries and re-embed undocumented functions."""
    async def _run():
        db, _, pipeline = await _make_indexer()
        try:
            return await pipeline.enrich_summaries(project_id, limit=limit, force=force)
        finally:
            await db.close()
    return asyncio.run(_run())


def run_reembed_project(project_id: str) -> dict:
    """RQ job: re-embed all functions for a project."""
    async def _run():
        db, indexer, _ = await _make_indexer()
        try:
            return await indexer.reembed_project(project_id)
        finally:
            await db.close()
    return asyncio.run(_run())
