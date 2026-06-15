from __future__ import annotations

import asyncio
import hashlib
import os
import re
from collections import OrderedDict
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from .chunker import FunctionChunk, prepare_embed_text

SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_CONCURRENCY = 10  # max parallel LLM summary calls

_KNOWN_DIMS: dict[str, int] = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-code": 768,
    "nomic-embed-text": 768,
    "mxbai-embed-large": 1024,
    "all-minilm": 384,
}

_PROVIDER_DEFAULTS: dict[str, tuple[str, int]] = {
    "openai": ("text-embedding-3-small", 1536),
    "ollama": ("nomic-embed-code", 768),
}


def _resolve_config() -> tuple[str, str, int, str]:
    """Read embedding provider, model, dimension, and Ollama URL from config file then env."""
    try:
        from ..web.config_store import read_file_config
        file_cfg = read_file_config()
    except Exception:
        file_cfg = {}

    def _get(key: str, default: str = "") -> str:
        val = file_cfg.get(key)
        if val:
            return val
        env_val = os.getenv(key, "")
        return env_val if env_val else default

    provider = _get("EMBEDDING_PROVIDER", "openai").lower()
    default_model, default_dim = _PROVIDER_DEFAULTS.get(provider, ("text-embedding-3-small", 1536))
    model = _get("EMBEDDING_MODEL", default_model)
    raw_dim = _get("EMBEDDING_DIM", "")
    dim = int(raw_dim) if raw_dim else _KNOWN_DIMS.get(model, default_dim)
    ollama_url = _get("OLLAMA_BASE_URL", "http://localhost:11434")
    return provider, model, dim, ollama_url


def _make_embed_client(provider: str, ollama_base_url: str) -> AsyncOpenAI:
    """Instantiate an AsyncOpenAI-compatible client for the configured provider."""
    if provider == "ollama":
        return AsyncOpenAI(base_url=f"{ollama_base_url}/v1", api_key="ollama")
    return AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


class EmbeddingStore:
    """
    Manages all vector embeddings using pgvector — stored in Postgres.

    Single shared `function_embeddings(project_id, id, embedding vector(N))` table
    for function embeddings, with HNSW index for fast ANN search.

    `decision_embeddings(id, embedding vector(N))` for decision memory.

    Per-contract dynamic tables for semantic contract checking:
      contract_violation_{safe_id}  /  contract_compliance_{safe_id}

    The connection pool is owned by CallGraphDB; this class borrows _db (_DB wrapper).
    """

    def __init__(self, db) -> None:
        """Initialize with a CallGraphDB reference and resolve embedding configuration."""
        self._db = db
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self._provider, self._model, self._dim, ollama_url = _resolve_config()
        self._embed_client = _make_embed_client(self._provider, ollama_url)
        # Always OpenAI for large-model fallback — used for undocumented functions
        self._large_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self._large_model = "text-embedding-3-large"
        # In-process LRU cache for _embed_single — keyed by (text_hash, model).
        self._embed_cache: OrderedDict[tuple[str, str], list[float]] = OrderedDict()
        self._embed_cache_max = 512
        print(f"[embeddings] provider={self._provider} model={self._model} dim={self._dim}")

    @classmethod
    async def create(cls, db) -> "EmbeddingStore":
        """Async factory — create and fully initialize an EmbeddingStore instance."""
        obj = cls(db)
        await obj.init()
        return obj

    async def init(self) -> None:
        """No-op — schema is in schema.sql, codec registered in CallGraphDB.init()."""
        pass

    async def close(self) -> None:
        """No-op — the pool is owned and closed by CallGraphDB."""
        pass

    # ── Shared ─────────────────────────────────────────────────────────────

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string. Used directly by Layer 3."""
        return await self._embed_single(text)

    # ── Layer 2: function embeddings ───────────────────────────────────────

    async def upsert_vector(
        self, node_id: str, vector: list[float], project_id: str
    ) -> None:
        """Upsert one embedding vector into function_embeddings."""
        conn = self._db._db
        await conn.execute(
            """INSERT INTO function_embeddings(project_id, id, embedding)
               VALUES(?, ?, ?)
               ON CONFLICT(project_id, id) DO UPDATE SET embedding = excluded.embedding""",
            (project_id, node_id, vector),
        )

    async def delete_by_file(self, file_path: str, project_id: str) -> None:
        """Remove embedding vectors for all functions belonging to a source file."""
        conn = self._db._db
        async with conn.execute(
            "SELECT id FROM nodes WHERE file = ? AND project_id = ?",
            (file_path, project_id),
        ) as cur:
            node_ids = [row[0] for row in await cur.fetchall()]
        if node_ids:
            await self.delete_by_ids(node_ids, project_id)

    async def delete_by_ids(self, function_ids: list[str], project_id: str) -> None:
        """Remove embedding vectors for specific function IDs from a project."""
        if not function_ids:
            return
        conn = self._db._db
        ph = ",".join("?" * len(function_ids))
        await conn.execute(
            f"DELETE FROM function_embeddings WHERE project_id = ? AND id IN ({ph})",
            (project_id, *function_ids),
        )

    async def get_embedded_ids(self, project_id: str) -> set:
        """Return the set of function IDs that currently have an embedding vector."""
        conn = self._db._db
        async with conn.execute(
            "SELECT id FROM function_embeddings WHERE project_id = ?",
            (project_id,),
        ) as cur:
            return {row[0] for row in await cur.fetchall()}

    async def query_similar(
        self, snippet: str, top_k: int = 10, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        """KNN search returning the top-k functions semantically similar to a code snippet."""
        embedding = await self._embed_single(snippet)
        conn = self._db._db

        if project_id:
            async with conn.execute(
                """SELECT fe.id, (fe.embedding <=> ?) AS distance,
                          n.file, n.module, n.name, n.signature, n.summary, n.project_id
                   FROM function_embeddings fe
                   JOIN nodes n ON n.id = fe.id AND n.project_id = fe.project_id
                   WHERE fe.project_id = ?
                   ORDER BY distance LIMIT ?""",
                (embedding, project_id, top_k),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]
        else:
            async with conn.execute(
                """SELECT fe.id, (fe.embedding <=> ?) AS distance,
                          n.file, n.module, n.name, n.signature, n.summary, fe.project_id
                   FROM function_embeddings fe
                   JOIN nodes n ON n.id = fe.id AND n.project_id = fe.project_id
                   ORDER BY distance LIMIT ?""",
                (embedding, top_k),
            ) as cur:
                rows = [dict(r) for r in await cur.fetchall()]

        for r in rows:
            r["similarity"] = round(1.0 - r["distance"] / 2.0, 4)
        return rows

    async def count_embeddings(self, project_id: str | None = None) -> int:
        """Count embedded functions for a project, or across all projects if unscoped."""
        conn = self._db._db
        if project_id:
            async with conn.execute(
                "SELECT COUNT(*) FROM function_embeddings WHERE project_id = ?",
                (project_id,),
            ) as cur:
                return (await cur.fetchone())[0]
        async with conn.execute("SELECT COUNT(*) FROM function_embeddings") as cur:
            return (await cur.fetchone())[0]

    async def count_embeddings_by_project(self) -> dict[str, int]:
        """Return embedding counts keyed by project_id in a single query."""
        conn = self._db._db
        async with conn.execute(
            "SELECT project_id, COUNT(*) FROM function_embeddings GROUP BY project_id"
        ) as cur:
            return {row[0]: row[1] for row in await cur.fetchall()}

    async def get_summaries(
        self, function_ids: list[str], project_id: str
    ) -> dict[str, str]:
        """Fetch the LLM-generated summaries for a list of function IDs in a project."""
        if not function_ids:
            return {}
        conn = self._db._db
        ph = ",".join("?" * len(function_ids))
        async with conn.execute(
            f"SELECT id, summary FROM nodes WHERE id IN ({ph}) AND project_id = ?",
            [*function_ids, project_id],
        ) as cur:
            return {r[0]: r[1] for r in await cur.fetchall()}

    # ── Layer 3: decision embeddings ───────────────────────────────────────

    async def upsert_decision_embedding(self, decision_id: str, text: str) -> None:
        """Embed a decision's reasoning text and store it in the decision_embeddings table."""
        embedding = await self._embed_single(text)
        conn = self._db._db
        await conn.execute(
            """INSERT INTO decision_embeddings(id, embedding) VALUES(?, ?)
               ON CONFLICT(id) DO UPDATE SET embedding = excluded.embedding""",
            (decision_id, embedding),
        )

    async def query_decision_embeddings(
        self, query_text: str, top_k: int = 10
    ) -> list[dict[str, Any]]:
        """KNN search over decision reasoning embeddings; returns id and distance."""
        embedding = await self._embed_single(query_text)
        conn = self._db._db
        async with conn.execute(
            "SELECT id, (embedding <=> ?) AS distance FROM decision_embeddings ORDER BY distance LIMIT ?",
            (embedding, top_k),
        ) as cur:
            return [{"id": r[0], "distance": r[1]} for r in await cur.fetchall()]

    async def delete_decision_embedding(self, decision_id: str) -> None:
        """Remove a decision's embedding from the decision_embeddings table."""
        conn = self._db._db
        await conn.execute(
            "DELETE FROM decision_embeddings WHERE id = ?", (decision_id,)
        )

    # ── Layer 4: contract embeddings ───────────────────────────────────────

    def _contract_table(self, contract_id: str, kind: str) -> str:
        """Return the Postgres table name for a contract's violation or compliance examples."""
        safe = re.sub(r"[^a-zA-Z0-9]", "_", contract_id)
        return f"contract_{kind}_{safe}"

    async def _ensure_contract_tables(self, conn, contract_id: str) -> tuple[str, str]:
        """Create violation and compliance tables for a contract; return their names."""
        viol_table = self._contract_table(contract_id, "violation")
        comp_table = self._contract_table(contract_id, "compliance")
        for table in (viol_table, comp_table):
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id        TEXT PRIMARY KEY,
                    embedding vector({self._dim})
                )
            """)
        return viol_table, comp_table

    async def upsert_contract_embeddings(
        self,
        contract_id: str,
        violation_codes: list[str],
        compliance_codes: list[str],
    ) -> None:
        """Embed violation/compliance code examples and store in per-contract tables."""
        import uuid
        conn = self._db._db
        viol_table, comp_table = await self._ensure_contract_tables(conn, contract_id)

        await conn.execute(f"DELETE FROM {viol_table}")
        await conn.execute(f"DELETE FROM {comp_table}")

        all_codes = violation_codes + compliance_codes
        if not all_codes:
            return

        embeddings = await self._embed_batch(all_codes)
        viol_embs = embeddings[:len(violation_codes)]
        comp_embs = embeddings[len(violation_codes):]

        for emb in viol_embs:
            await conn.execute(
                f"INSERT INTO {viol_table}(id, embedding) VALUES(?, ?)",
                (str(uuid.uuid4()), emb),
            )
        for emb in comp_embs:
            await conn.execute(
                f"INSERT INTO {comp_table}(id, embedding) VALUES(?, ?)",
                (str(uuid.uuid4()), emb),
            )

    async def delete_project_embeddings(self, project_id: str) -> None:
        """Remove all embedding vectors for a project."""
        conn = self._db._db
        await conn.execute(
            "DELETE FROM function_embeddings WHERE project_id = ?", (project_id,)
        )

    async def delete_contract_embeddings(self, contract_id: str) -> None:
        """Drop the violation and compliance tables for a deleted contract."""
        conn = self._db._db
        for kind in ("violation", "compliance"):
            table = self._contract_table(contract_id, kind)
            await conn.execute(f"DROP TABLE IF EXISTS {table}")

    async def check_semantic(
        self, contract_id: str, code_snippet: str
    ) -> tuple[bool, float, float]:
        """
        Check whether code_snippet violates a contract semantically.
        Returns (is_violation, violation_score, compliance_score).
        """
        conn = self._db._db
        viol_table = self._contract_table(contract_id, "violation")
        comp_table = self._contract_table(contract_id, "compliance")

        # Check both tables exist in the Postgres catalog.
        for table in (viol_table, comp_table):
            async with conn.execute(
                "SELECT 1 FROM pg_tables WHERE tablename = ?", (table,)
            ) as cur:
                if not await cur.fetchone():
                    return False, 0.0, 0.0

        async with conn.execute(f"SELECT COUNT(*) FROM {viol_table}") as cur:
            if (await cur.fetchone())[0] == 0:
                return False, 0.0, 0.0

        embedding = await self._embed_single(code_snippet)

        async with conn.execute(
            f"SELECT (embedding <=> ?) AS distance FROM {viol_table} ORDER BY distance LIMIT 1",
            (embedding,),
        ) as cur:
            row = await cur.fetchone()
        viol_score = round(1.0 - row[0] / 2.0, 4) if row else 0.0

        async with conn.execute(f"SELECT COUNT(*) FROM {comp_table}") as cur:
            comp_count = (await cur.fetchone())[0]
        comp_score = 0.0
        if comp_count > 0:
            async with conn.execute(
                f"SELECT (embedding <=> ?) AS distance FROM {comp_table} ORDER BY distance LIMIT 1",
                (embedding,),
            ) as cur:
                row = await cur.fetchone()
            comp_score = round(1.0 - row[0] / 2.0, 4) if row else 0.0

        async with conn.execute(
            "SELECT threshold FROM contracts WHERE id = ?", (contract_id,)
        ) as cur:
            crow = await cur.fetchone()
        threshold = crow[0] if crow else 0.85

        is_violation = viol_score >= threshold and comp_score < threshold
        return is_violation, viol_score, comp_score

    # ── Internals ──────────────────────────────────────────────────────────

    async def _embed_single(self, text: str) -> list[float]:
        """Embed a single text string via the configured provider and return the float vector."""
        key = (hashlib.sha256(text.encode()).hexdigest()[:16], self._model)
        if key in self._embed_cache:
            self._embed_cache.move_to_end(key)
            return self._embed_cache[key]
        resp = await self._embed_client.embeddings.create(model=self._model, input=text)
        vec = resp.data[0].embedding
        self._embed_cache[key] = vec
        if len(self._embed_cache) > self._embed_cache_max:
            self._embed_cache.popitem(last=False)
        return vec

    async def _embed_batch_large(self, texts: list[str]) -> list[list[float]]:
        """Embed using text-embedding-3-large truncated to self._dim for table compatibility."""
        results = []
        for i in range(0, len(texts), 100):
            batch = texts[i:i + 100]
            resp = await self._large_client.embeddings.create(
                model=self._large_model,
                input=batch,
                dimensions=self._dim,
            )
            results.extend(r.embedding for r in sorted(resp.data, key=lambda x: x.index))
        return results

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts in batches of 100, logging progress per batch."""
        if not texts:
            return []
        results: list[list[float]] = []
        total_batches = (len(texts) + 99) // 100
        for i in range(0, len(texts), 100):
            batch = texts[i:i + 100]
            batch_num = i // 100 + 1
            print(f"[embeddings]   embed batch {batch_num}/{total_batches} "
                  f"({len(batch)} texts) → {self._provider}/{self._model}")
            resp = await self._embed_client.embeddings.create(model=self._model, input=batch)
            results.extend(item.embedding for item in sorted(resp.data, key=lambda x: x.index))
            print(f"[embeddings]   batch {batch_num}/{total_batches} ok")
        return results

    async def _generate_summary(self, chunk: FunctionChunk) -> str:
        """Generate a one-sentence LLM summary for a function chunk via Claude Haiku."""
        prompt = (
            f"Write a single sentence describing what this function does.\n\n"
            f"Function: {chunk.id}\nSignature: {chunk.signature}\n"
        )
        if chunk.docstring:
            prompt += f"Docstring: {chunk.docstring}\n"
        prompt += f"\nBody (truncated):\n{chunk.body[:800]}"
        try:
            resp = await self._anthropic.messages.create(
                model=SUMMARY_MODEL, max_tokens=80,
                messages=[{"role": "user", "content": prompt}],
            )
            summary = resp.content[0].text.strip()
            print(f"[embeddings]     summary: {chunk.id[:60]} → {summary[:60]}")
            return summary
        except Exception as exc:
            fallback = chunk.docstring[:200] if chunk.docstring else chunk.signature
            print(f"[embeddings]     summary failed ({exc}), using fallback")
            return fallback
