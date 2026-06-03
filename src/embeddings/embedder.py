from __future__ import annotations

import os
from typing import Any

from anthropic import AsyncAnthropic
from neo4j import AsyncGraphDatabase, AsyncDriver
from openai import AsyncOpenAI

from .chunker import FunctionChunk, prepare_embed_text

SUMMARY_MODEL = "claude-haiku-4-5-20251001"

# ── Embedding config ───────────────────────────────────────────────────────────
# Known vector dimensions per model name. Add entries here as you add models.
_KNOWN_DIMS: dict[str, int] = {
    # OpenAI
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    # Ollama (nomic, mxbai, etc.)
    "nomic-embed-code": 768,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}

# Sensible defaults per provider when the user hasn't set the model explicitly
_PROVIDER_DEFAULTS: dict[str, tuple[str, int]] = {
    "openai": ("text-embedding-3-small", 1536),
    "ollama": ("nomic-embed-code", 768),
}


def _resolve_config() -> tuple[str, str, int, str]:
    """Return (provider, model, dim, ollama_base_url).
    config.json (written by the web UI) takes precedence over env vars."""
    try:
        from ..web.config_store import read_file_config
        file_cfg = read_file_config()
    except Exception:
        file_cfg = {}

    def _get(key: str, default: str = "") -> str:
        return file_cfg.get(key) or os.getenv(key, default)

    provider = _get("EMBEDDING_PROVIDER", "openai").lower()
    default_model, default_dim = _PROVIDER_DEFAULTS.get(provider, ("text-embedding-3-small", 1536))
    model = _get("EMBEDDING_MODEL", default_model)
    raw_dim = _get("EMBEDDING_DIM", "")
    dim = int(raw_dim) if raw_dim else _KNOWN_DIMS.get(model, default_dim)
    ollama_url = _get("OLLAMA_BASE_URL", "http://localhost:11434")
    return provider, model, dim, ollama_url


def _make_embed_client(provider: str, ollama_base_url: str) -> AsyncOpenAI:
    """
    Both OpenAI and Ollama are accessed via the OpenAI-compatible client.
    Ollama exposes /v1/embeddings at its base URL.
    """
    if provider == "ollama":
        return AsyncOpenAI(base_url=f"{ollama_base_url}/v1", api_key="ollama")
    return AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


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
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        self._provider, self._model, self._dim, ollama_url = _resolve_config()
        self._embed_client = _make_embed_client(self._provider, ollama_url)

        print(
            f"[embeddings] provider={self._provider} model={self._model} dim={self._dim}"
        )

    @classmethod
    async def create(cls, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> "EmbeddingStore":
        obj = cls(neo4j_uri, neo4j_user, neo4j_password)
        await obj.init()
        return obj

    async def init(self) -> None:
        self._driver = AsyncGraphDatabase.driver(self._uri, auth=(self._user, self._password))
        async with self._driver.session() as session:
            # NOTE: If you change EMBEDDING_MODEL/EMBEDDING_DIM after the index was
            # already created, you must drop the old index first:
            #   MATCH (n:Function) DETACH DELETE n;
            #   DROP INDEX function_embeddings;
            # Then restart the server to recreate it at the new dimension.
            await session.run(_CREATE_VECTOR_INDEX, dim=self._dim)

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()

    # ── Public API ─────────────────────────────────────────────────────────

    async def upsert_chunks(
        self,
        chunks: list[FunctionChunk],
        existing_summaries: dict[str, str] | None = None,
        force_summaries: bool = False,
    ) -> None:
        """Embed and store each chunk. Generates LLM summary on first embed.
        Pass force_summaries=True to regenerate summaries even for known functions."""
        if not chunks:
            return

        # Attach existing summaries unless forcing a regeneration
        if existing_summaries and not force_summaries:
            for chunk in chunks:
                if not chunk.summary and chunk.id in existing_summaries:
                    chunk.summary = existing_summaries[chunk.id]

        # Generate missing (or forced) summaries
        for chunk in chunks:
            if not chunk.summary or force_summaries:
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
        resp = await self._embed_client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # OpenAI supports batch up to 2048 inputs; chunk for safety
        results: list[list[float]] = []
        batch_size = 100
        for i in range(0, len(texts), batch_size):
            batch = texts[i : i + batch_size]
            resp = await self._embed_client.embeddings.create(model=self._model, input=batch)
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
