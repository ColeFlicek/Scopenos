"""
RQ job functions for long-running Scopenos operations.

Each function is a synchronous wrapper around async indexing code.
Workers run these in a separate process with their own DB pool.
All functions must be importable at module level (RQ serialises them by name).

The caller must pass db_url explicitly — resolved from the org's database URL
via OrgRouter before the job is enqueued. CONTROL_DB_URL is the fallback only
for admin/CLI-triggered jobs that run outside the HTTP request context.
"""
from __future__ import annotations

import asyncio
import os


def _resolve_dsn(db_url: str | None) -> str:
    if db_url:
        return db_url
    dsn = os.getenv("CONTROL_DB_URL")
    if not dsn:
        raise RuntimeError(
            "db_url must be passed explicitly or CONTROL_DB_URL must be set. "
            "DATABASE_URL is no longer supported."
        )
    return dsn


async def _make_indexer(db_url: str | None = None, skip_schema_init: bool = False):
    from .call_graph.storage import CallGraphDB
    from .embeddings.embedder import EmbeddingStore
    from .embeddings.pipeline import EmbeddingPipeline
    from .indexer import Indexer
    db = await CallGraphDB.create(_resolve_dsn(db_url), skip_schema_init=skip_schema_init)
    embeddings = await EmbeddingStore.create(db)
    pipeline = EmbeddingPipeline(db, embeddings)
    return db, Indexer(db, pipeline), pipeline


def run_index_project(path: str, project_id: str, db_url: str | None = None) -> dict:
    """RQ job: full project index.

    db_url must be the org's database URL in multi-org deployments. Omit only
    in single-tenant mode where CONTROL_DB_URL / DATABASE_URL is the org DB.
    """
    async def _run():
        db, indexer, _ = await _make_indexer(db_url)
        try:
            return await indexer.index_project(path, project_id=project_id)
        finally:
            await db.close()
    return asyncio.run(_run())


def run_enrich_summaries(
    project_id: str, limit: int = 500, force: bool = False, db_url: str | None = None
) -> dict:
    """RQ job: generate LLM summaries and re-embed undocumented functions."""
    async def _run():
        db, _, pipeline = await _make_indexer(db_url, skip_schema_init=True)
        try:
            return await pipeline.enrich_summaries(project_id, limit=limit, force=force)
        finally:
            await db.close()
    return asyncio.run(_run())


def run_reembed_project(project_id: str, db_url: str | None = None) -> dict:
    """RQ job: re-embed all functions for a project."""
    async def _run():
        db, indexer, _ = await _make_indexer(db_url, skip_schema_init=True)
        try:
            return await indexer.reembed_project(project_id)
        finally:
            await db.close()
    return asyncio.run(_run())
