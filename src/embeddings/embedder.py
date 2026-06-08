from __future__ import annotations

import asyncio
import os
import re
import struct
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
    if provider == "ollama":
        return AsyncOpenAI(base_url=f"{ollama_base_url}/v1", api_key="ollama")
    return AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def _load_vec_ext(conn) -> None:
    """Load sqlite-vec extension into a raw sqlite3 connection."""
    import sqlite_vec
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)


def _f32(v: list[float]) -> bytes:
    """Serialize a float list to IEEE 754 float32 bytes for sqlite-vec."""
    return struct.pack(f"{len(v)}f", *v)


def _safe_name(project_id: str) -> str:
    """Sanitize project_id for use as an SQLite identifier suffix."""
    return re.sub(r"[^a-zA-Z0-9]", "_", project_id)


def _emb_table(project_id: str) -> str:
    """Return the vec0 table name for a project's function embeddings."""
    return f"function_embeddings_{_safe_name(project_id)}"


class EmbeddingStore:
    """
    Manages all vector embeddings using sqlite-vec — no external server required.

    Per-project vec0 virtual tables for function embeddings:
      - function_embeddings_{project_id}  (one per project, created on first index)

    Single shared table for decision embeddings:
      - decision_embeddings  (decisions use UUIDs — globally unique, no prefix needed)

    The connection is owned by CallGraphDB; this class borrows it.
    """

    def __init__(self, db) -> None:  # db: CallGraphDB (avoid circular import at type level)
        self._db = db
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self._provider, self._model, self._dim, ollama_url = _resolve_config()
        self._embed_client = _make_embed_client(self._provider, ollama_url)
        print(f"[embeddings] provider={self._provider} model={self._model} dim={self._dim}")

    @classmethod
    async def create(cls, db) -> "EmbeddingStore":
        obj = cls(db)
        await obj.init()
        return obj

    async def init(self) -> None:
        conn = self._db._db
        _load_vec_ext(conn._connection)

        await conn.execute(
            "CREATE TABLE IF NOT EXISTS _embedding_meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        async with conn.execute(
            "SELECT value FROM _embedding_meta WHERE key = 'embedding_dim'"
        ) as cur:
            row = await cur.fetchone()
        if row and int(row[0]) != self._dim:
            raise RuntimeError(
                f"Embedding dimension mismatch: stored index is {row[0]}d but configured "
                f"model needs {self._dim}d. Delete SQLITE_PATH and restart to re-index."
            )

        # Migrate old shared function_embeddings table to per-project tables.
        # The shared table was introduced in the multi-project migration but is now
        # superseded by per-project tables for true query isolation.
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='function_embeddings'"
        ) as cur:
            if await cur.fetchone():
                await self._migrate_shared_to_per_project(conn)

        # decision_embeddings stays as a single shared table (UUID IDs, no collision risk).
        await conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS decision_embeddings USING vec0(
                id TEXT PRIMARY KEY,
                embedding FLOAT[{self._dim}]
            )
        """)
        await conn.execute(
            "INSERT OR REPLACE INTO _embedding_meta(key, value) VALUES ('embedding_dim', ?)",
            (str(self._dim),)
        )
        await conn.commit()

    async def _migrate_shared_to_per_project(self, conn) -> None:
        """
        Migrate from shared function_embeddings (with {project_id}::{node_id} IDs)
        to per-project function_embeddings_{project_id} tables.
        Reads all embedding blobs directly — no re-embedding required.
        """
        print("[embeddings] Migrating shared function_embeddings → per-project tables...")
        async with conn.execute("SELECT id, embedding FROM function_embeddings") as cur:
            rows = await cur.fetchall()

        by_project: dict[str, list[tuple[str, bytes]]] = {}
        skipped = 0
        for row_id, emb_blob in rows:
            row_id = str(row_id)
            if "::" in row_id:
                pid, node_id = row_id.split("::", 1)
                by_project.setdefault(pid, []).append((node_id, emb_blob))
            else:
                skipped += 1  # old single-project format without prefix — unusable

        for pid, vectors in by_project.items():
            table = _emb_table(pid)
            await conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
                    id TEXT PRIMARY KEY,
                    embedding FLOAT[{self._dim}]
                )
            """)
            for node_id, emb_blob in vectors:
                await conn.execute(
                    f"INSERT OR REPLACE INTO {table}(id, embedding) VALUES (?, ?)",
                    (node_id, emb_blob),
                )

        await conn.execute("DROP TABLE function_embeddings")
        await conn.commit()
        print(f"[embeddings] Migrated {len(rows) - skipped} vectors across "
              f"{len(by_project)} projects ({skipped} stale entries dropped)")

    async def _ensure_emb_table(self, conn, project_id: str) -> str:
        """Create project-specific embedding table if not exists. Returns table name."""
        table = _emb_table(project_id)
        await conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
                id TEXT PRIMARY KEY,
                embedding FLOAT[{self._dim}]
            )
        """)
        return table

    async def close(self) -> None:
        pass  # connection owned by CallGraphDB

    # ── Shared ─────────────────────────────────────────────────────────────

    async def embed(self, text: str) -> list[float]:
        """Embed a single text string. Used directly by Layer 3."""
        return await self._embed_single(text)

    # ── Layer 2: function embeddings ───────────────────────────────────────

    async def upsert_chunks(
        self,
        chunks: list[FunctionChunk],
        project_id: str,
        existing_summaries: dict[str, str] | None = None,
        force_summaries: bool = False,
    ) -> None:
        if not chunks:
            return

        cached_count = 0
        if existing_summaries and not force_summaries:
            for chunk in chunks:
                if not chunk.summary and chunk.id in existing_summaries:
                    cached = existing_summaries[chunk.id]
                    if cached:
                        chunk.summary = cached
                        cached_count += 1

        needs_summary = [c for c in chunks if not c.summary or force_summaries]
        print(f"[embeddings] summarising {len(needs_summary)}/{len(chunks)} functions "
              f"({cached_count} summaries reused from cache)")

        if needs_summary:
            # Parallel summarization bounded by SUMMARY_CONCURRENCY.
            sem = asyncio.Semaphore(SUMMARY_CONCURRENCY)

            async def _summarize(chunk: FunctionChunk) -> str:
                async with sem:
                    return await self._generate_summary(chunk)

            summaries = await asyncio.gather(*[_summarize(c) for c in needs_summary])
            for chunk, summary in zip(needs_summary, summaries):
                chunk.summary = summary

        print(f"[embeddings] embedding {len(chunks)} functions via {self._provider}/{self._model}")
        texts = [prepare_embed_text(chunk) for chunk in chunks]
        embeddings = await self._embed_batch(texts)

        conn = self._db._db
        table = await self._ensure_emb_table(conn, project_id)
        for chunk, embedding in zip(chunks, embeddings):
            await conn.execute(
                "UPDATE nodes SET summary = ? WHERE id = ? AND project_id = ?",
                (chunk.summary, chunk.id, project_id),
            )
            await conn.execute(
                f"INSERT OR REPLACE INTO {table}(id, embedding) VALUES (?, ?)",
                (chunk.id, _f32(embedding)),
            )
        await conn.commit()
        print(f"[embeddings] stored {len(chunks)} embeddings ok")

    async def delete_by_file(self, file_path: str, project_id: str) -> None:
        conn = self._db._db
        async with conn.execute(
            "SELECT id FROM nodes WHERE file = ? AND project_id = ?",
            (file_path, project_id),
        ) as cur:
            node_ids = [row[0] for row in await cur.fetchall()]
        if node_ids:
            await self.delete_by_ids(node_ids, project_id)

    async def delete_by_ids(self, function_ids: list[str], project_id: str) -> None:
        if not function_ids:
            return
        table = _emb_table(project_id)
        conn = self._db._db
        # Table may not exist yet for brand-new projects.
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ) as cur:
            if not await cur.fetchone():
                return
        ph = ",".join("?" * len(function_ids))
        await conn.execute(f"DELETE FROM {table} WHERE id IN ({ph})", function_ids)
        await conn.commit()

    async def query_similar(
        self, snippet: str, top_k: int = 10, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        embedding = await self._embed_single(snippet)
        conn = self._db._db

        if project_id:
            # Single-project query: scan only that project's table.
            table = _emb_table(project_id)
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                if not await cur.fetchone():
                    return []
            async with conn.execute(
                f"""
                SELECT knn.id, knn.distance,
                       n.file, n.module, n.name, n.signature, n.summary, n.project_id
                FROM (
                    SELECT id, distance FROM {table}
                    WHERE embedding MATCH ? AND k = ?
                    ORDER BY distance
                ) knn
                JOIN nodes n ON n.id = knn.id AND n.project_id = ?
                """,
                (_f32(embedding), top_k, project_id),
            ) as cur:
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in await cur.fetchall()]
        else:
            # Cross-project query: UNION across all project tables via a CTE,
            # then JOIN to nodes using (id, project_id) to avoid cross-project collisions.
            async with conn.execute(
                "SELECT id FROM projects ORDER BY created_at"
            ) as cur:
                project_ids = [row[0] for row in await cur.fetchall()]

            # Build list of tables that actually exist.
            valid: list[tuple[str, str]] = []
            for pid in project_ids:
                t = _emb_table(pid)
                async with conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (t,)
                ) as cur:
                    if await cur.fetchone():
                        valid.append((pid, t))

            if not valid:
                return []

            # CTE: each project contributes top_k candidates; global ORDER BY picks the best.
            parts = [
                f"SELECT ? AS pid, id, distance FROM {t} WHERE embedding MATCH ? AND k = ?"
                for _, t in valid
            ]
            params: list[Any] = []
            for pid, _ in valid:
                params.extend([pid, _f32(embedding), top_k])
            params.append(top_k)

            cte_sql = " UNION ALL ".join(parts)
            sql = f"""
                WITH knn AS (
                    {cte_sql}
                    ORDER BY distance LIMIT ?
                )
                SELECT knn.id, knn.distance, knn.pid AS project_id,
                       n.file, n.module, n.name, n.signature, n.summary
                FROM knn
                JOIN nodes n ON n.id = knn.id AND n.project_id = knn.pid
            """
            async with conn.execute(sql, params) as cur:
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, row)) for row in await cur.fetchall()]

        # Normalize L2 distance [0, 2] → similarity [0, 1] for unit-normalized embeddings.
        for r in rows:
            r["similarity"] = round(1.0 - r["distance"] / 2.0, 4)
        return rows

    async def count_embeddings(self, project_id: str | None = None) -> int:
        """Count embedded functions. If project_id given, count only that project."""
        conn = self._db._db
        if project_id:
            table = _emb_table(project_id)
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                if not await cur.fetchone():
                    return 0
            async with conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
                return (await cur.fetchone())[0]
        # Sum across known projects only — avoids counting vec0 shadow tables
        # (_info, _rowids, _chunks, etc.) that also match 'function_embeddings_%'.
        async with conn.execute("SELECT id FROM projects") as cur:
            project_ids = [row[0] for row in await cur.fetchall()]
        total = 0
        for pid in project_ids:
            total += await self.count_embeddings(pid)
        return total

    async def get_summaries(
        self, function_ids: list[str], project_id: str
    ) -> dict[str, str]:
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
        embedding = await self._embed_single(text)
        conn = self._db._db
        await conn.execute(
            "INSERT OR REPLACE INTO decision_embeddings(id, embedding) VALUES (?, ?)",
            (decision_id, _f32(embedding)),
        )
        await conn.commit()

    async def query_decision_embeddings(
        self, query_text: str, top_k: int = 10
    ) -> list[dict[str, Any]]:
        embedding = await self._embed_single(query_text)
        conn = self._db._db
        async with conn.execute(
            """
            SELECT id, distance
            FROM decision_embeddings
            WHERE embedding MATCH ? AND k = ?
            ORDER BY distance
            """,
            (_f32(embedding), top_k),
        ) as cur:
            return [{"id": r[0], "distance": r[1]} for r in await cur.fetchall()]

    async def delete_decision_embedding(self, decision_id: str) -> None:
        conn = self._db._db
        await conn.execute(
            "DELETE FROM decision_embeddings WHERE id = ?", (decision_id,)
        )
        await conn.commit()

    # ── Internals ──────────────────────────────────────────────────────────

    async def _embed_single(self, text: str) -> list[float]:
        resp = await self._embed_client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    async def _embed_batch(self, texts: list[str]) -> list[list[float]]:
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
