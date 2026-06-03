from __future__ import annotations

import os
from typing import Any

from anthropic import AsyncAnthropic
from neo4j import AsyncGraphDatabase, AsyncDriver
from openai import AsyncOpenAI

from .chunker import FunctionChunk, prepare_embed_text

EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIM = 1536
SUMMARY_MODEL = "claude-haiku-4-5-20251001"

_CREATE_VECTOR_INDEX = """
CREATE VECTOR INDEX function_embeddings IF NOT EXISTS
FOR (n:Function)
ON (n.embedding)
OPTIONS {
  indexConfig: {
    `vector.dimensions`: $dim,
    `vector.similarity_function`: 'cosine'
  }
}
"""

_UPSERT_FUNCTION = """
MERGE (n:Function {id: $id})
SET n.file      = $file,
    n.module    = $module,
    n.type      = $type,
    n.name      = $name,
    n.signature = $signature,
    n.summary   = $summary,
    n.embedding = $embedding
"""

_QUERY_SIMILAR = """
CALL db.index.vector.queryNodes('function_embeddings', $top_k, $embedding)
YIELD node, score
RETURN node.id AS id,
       node.file AS file,
       node.module AS module,
       node.name AS name,
       node.signature AS signature,
       node.summary AS summary,
       score
ORDER BY score DESC
"""

_DELETE_BY_FILE = """
MATCH (n:Function {file: $file}) DETACH DELETE n
"""

_DELETE_BY_IDS = """
MATCH (n:Function) WHERE n.id IN $ids DETACH DELETE n
"""


class EmbeddingStore:
    def __init__(self, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> None:
        self._uri = neo4j_uri
        self._user = neo4j_user
        self._password = neo4j_password
        self._driver: AsyncDriver | None = None
        self._openai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    @classmethod
    async def create(cls, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> "EmbeddingStore":
        obj = cls(neo4j_uri, neo4j_user, neo4j_password)
        await obj.init()
        return obj

    async def init(self) -> None:
        self._driver = AsyncGraphDatabase.driver(self._uri, auth=(self._user, self._password))
        async with self._driver.session() as session:
            await session.run(_CREATE_VECTOR_INDEX, dim=EMBEDDING_DIM)

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    # ── Public API ─────────────────────────────────────────────────────────

    async def upsert_chunks(
        self, chunks: list[FunctionChunk], existing_summaries: dict[str, str] | None = None
    ) -> None:
        """Embed and store each chunk. Generates LLM summary on first embed."""
        if not chunks:
            return

        # Attach existing summaries to avoid re-generating
        if existing_summaries:
            for chunk in chunks:
                if not chunk.summary and chunk.id in existing_summaries:
                    chunk.summary = existing_summaries[chunk.id]

        # Generate missing summaries
        for chunk in chunks:
            if not chunk.summary:
                chunk.summary = await self._generate_summary(chunk)

        # Build embed texts and batch-embed
        texts = [prepare_embed_text(chunk) for chunk in chunks]
        embeddings = await self._embed_batch(texts)

        async with self._driver.session() as session:
            for chunk, embedding in zip(chunks, embeddings):
                await session.run(
                    _UPSERT_FUNCTION,
                    id=chunk.id,
                    file=chunk.file,
                    module=chunk.module,
                    type=chunk.type,
                    name=chunk.name,
                    signature=chunk.signature,
                    summary=chunk.summary,
                    embedding=embedding,
                )

    async def delete_by_file(self, file_path: str) -> None:
        async with self._driver.session() as session:
            await session.run(_DELETE_BY_FILE, file=file_path)

    async def delete_by_ids(self, function_ids: list[str]) -> None:
        if not function_ids:
            return
        async with self._driver.session() as session:
            await session.run(_DELETE_BY_IDS, ids=function_ids)

    async def query_similar(self, snippet: str, top_k: int = 10) -> list[dict[str, Any]]:
        embedding = await self._embed_single(snippet)
        async with self._driver.session() as session:
            result = await session.run(_QUERY_SIMILAR, embedding=embedding, top_k=top_k)
            return [dict(r) async for r in result]

    async def get_summaries(self, function_ids: list[str]) -> dict[str, str]:
        """Fetch existing summaries from neo4j to avoid re-generating on incremental updates."""
        if not function_ids:
            return {}
        async with self._driver.session() as session:
            result = await session.run(
                "MATCH (n:Function) WHERE n.id IN $ids RETURN n.id AS id, n.summary AS summary",
                ids=function_ids,
            )
            return {r["id"]: r["summary"] async for r in result}

    # ── Internals ──────────────────────────────────────────────────────────

    async def _embed_single(self, text: str) -> list[float]:
        resp = await self._openai.embeddings.create(model=EMBEDDING_MODEL, input=text)
        return resp.data[0].embedding

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # OpenAI supports batch up to 2048 inputs; chunk for safety
        results: list[list[float]] = []
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await self._openai.embeddings.create(model=EMBEDDING_MODEL, input=batch)
            results.extend(item.embedding for item in sorted(resp.data, key=lambda x: x.index))
        return results

    async def _generate_summary(self, chunk: FunctionChunk) -> str:
        prompt = (
            f"Write a single sentence describing what this function does.\n\n"
            f"Function: {chunk.id}\n"
            f"Signature: {chunk.signature}\n"
        )
        if chunk.docstring:
            prompt += f"Docstring: {chunk.docstring}\n"
        prompt += f"\nBody (truncated):\n{chunk.body[:800]}"

        try:
            resp = await self._anthropic.messages.create(
                model=SUMMARY_MODEL,
                max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.content[0].text.strip()
        except Exception:
            return chunk.docstring[:200] if chunk.docstring else chunk.signature
