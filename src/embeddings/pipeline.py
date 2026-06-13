from __future__ import annotations

import asyncio

from .chunker import FunctionChunk, prepare_embed_text
from .embedder import SUMMARY_CONCURRENCY, EmbeddingStore

# Avoid circular import at module level — CallGraphDB imported inside type hints only.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ..call_graph.storage import CallGraphDB


class EmbeddingPipeline:
    """
    Orchestrates the two-tier embedding strategy: classifies FunctionChunks by
    documentation coverage, routes to the appropriate embedding model, and writes
    results to both CallGraphDB (node metadata) and EmbeddingStore (vectors).

    This is the single place where the routing heuristic lives. Callers (Indexer,
    server tools) talk to the pipeline — never to EmbeddingStore directly for
    upsert or enrich operations.
    """

    def __init__(self, db: "CallGraphDB", store: EmbeddingStore) -> None:
        self._db = db
        self._store = store

    @property
    def model(self) -> str:
        return self._store._model

    # ── Pipeline operations ───────────────────────────────────────────────

    async def upsert_chunks(
        self,
        chunks: list[FunctionChunk],
        project_id: str,
        existing_summaries: dict[str, str] | None = None,
        force_summaries: bool = False,
    ) -> dict:
        """Two-tier embedding: documented chunks → small model; undocumented → large model."""
        if not chunks:
            return {"docs": 0, "fallback": 0}

        if existing_summaries and not force_summaries:
            for chunk in chunks:
                if not chunk.summary and chunk.id in existing_summaries:
                    cached = existing_summaries[chunk.id]
                    if cached:
                        chunk.summary = cached

        if not force_summaries:
            for chunk in chunks:
                if not chunk.summary and chunk.docstring:
                    chunk.summary = chunk.docstring[:200]

        doc_chunks = [c for c in chunks if c.summary or c.docstring or c.leading_comment]
        raw_chunks = [c for c in chunks if not (c.summary or c.docstring or c.leading_comment)]

        print(
            f"[embeddings] two-tier: {len(doc_chunks)} documented ({self._store._model}), "
            f"{len(raw_chunks)} undocumented ({self._store._large_model})"
        )

        if doc_chunks:
            texts = [prepare_embed_text(c) for c in doc_chunks]
            vectors = await self._store._embed_batch(texts)
            for chunk, vec in zip(doc_chunks, vectors):
                await self._db.update_node_embedding_meta(
                    chunk.id, chunk.summary, self._store._model, project_id
                )
                await self._store.upsert_vector(chunk.id, vec, project_id)

        if raw_chunks:
            texts = [prepare_embed_text(c) for c in raw_chunks]
            vectors = await self._store._embed_batch_large(texts)
            for chunk, vec in zip(raw_chunks, vectors):
                await self._db.update_node_embedding_meta(
                    chunk.id, None, self._store._large_model, project_id
                )
                await self._store.upsert_vector(chunk.id, vec, project_id)

        await self._db.commit()
        print(
            f"[embeddings] stored {len(chunks)} embeddings ok "
            f"({len(doc_chunks)} small-model, {len(raw_chunks)} large-model)"
        )
        return {"docs": len(doc_chunks), "fallback": len(raw_chunks)}

    async def enrich_summaries(self, project_id: str, limit: int = 500) -> dict:
        """LLM-summarize functions on the large-model fallback, then re-embed with the small model."""
        rows = await self._db.get_nodes_needing_enrichment(project_id, limit)

        if not rows:
            return {
                "enriched": 0,
                "remaining": 0,
                "message": "No functions need enrichment — all are already on the configured model.",
            }

        chunks = [
            FunctionChunk(
                id=r["id"], name=r["name"], signature=r["signature"],
                docstring=r["docstring"] or "", leading_comment="", summary="",
                file=r["file"], module="", type="function", body="", embed_text="",
            )
            for r in rows
        ]

        sem = asyncio.Semaphore(SUMMARY_CONCURRENCY)

        async def _summarize(chunk: FunctionChunk) -> str:
            async with sem:
                return await self._store._generate_summary(chunk)

        print(f"[embeddings] enriching {len(chunks)} functions with LLM summaries")
        summaries = await asyncio.gather(*[_summarize(c) for c in chunks])
        for chunk, summary in zip(chunks, summaries):
            chunk.summary = summary

        texts = [prepare_embed_text(c) for c in chunks]
        vectors = await self._store._embed_batch(texts)

        for chunk, vec in zip(chunks, vectors):
            await self._db.update_node_embedding_meta(
                chunk.id, chunk.summary, self._store._model, project_id
            )
            await self._store.upsert_vector(chunk.id, vec, project_id)

        await self._db.commit()

        remaining = await self._db.count_nodes_by_model(project_id, self._store._large_model)
        return {
            "enriched": len(chunks),
            "remaining": remaining,
            "message": (
                f"Enriched {len(chunks)} functions with LLM summaries and re-embedded with "
                f"{self._store._model}. "
                + (
                    f"{remaining} still use large-model fallback — call enrich_summaries again to continue."
                    if remaining
                    else "All functions are now on the configured model."
                )
            ),
        }

    # ── Delegations to EmbeddingStore ────────────────────────────────────

    async def delete_by_ids(self, function_ids: list[str], project_id: str) -> None:
        await self._store.delete_by_ids(function_ids, project_id)

    async def delete_by_file(self, file_path: str, project_id: str) -> None:
        await self._store.delete_by_file(file_path, project_id)

    async def get_summaries(self, function_ids: list[str], project_id: str) -> dict[str, str]:
        return await self._store.get_summaries(function_ids, project_id)

    async def get_embedded_ids(self, project_id: str) -> set[str]:
        """Return the set of function IDs that currently have an embedding vector."""
        return await self._store.get_embedded_ids(project_id)
