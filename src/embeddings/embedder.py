from __future__ import annotations

import os
import struct
from typing import Any

from anthropic import AsyncAnthropic
from openai import AsyncOpenAI

from .chunker import FunctionChunk, prepare_embed_text

SUMMARY_MODEL = "claude-haiku-4-5-20251001"

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


def _emb_id(project_id: str, node_id: str) -> str:
    """Embedding row ID format: '{project_id}::{node_id}'."""
    return f"{project_id}::{node_id}"


class EmbeddingStore:
    """
    Manages all vector embeddings using sqlite-vec — no external server required.

    Two vec0 virtual tables live in the same SQLite file as the call graph:
      - function_embeddings  (Layer 2): code similarity — function bodies + summaries
      - decision_embeddings  (Layer 3): reasoning similarity — intent + context

    Embedding IDs for functions are prefixed: "{project_id}::{node_id}".
    This keeps per-project vectors isolated inside a shared vec0 table.
    Decision embedding IDs are plain UUIDs (globally unique, no prefix needed).

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

        # Detect dimension mismatch before creating tables
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

        # Detect old single-project embedding IDs (no '::' prefix) after schema migration.
        # These vectors are now unreachable — wipe and let re-index regenerate them.
        try:
            async with conn.execute(
                "SELECT id FROM function_embeddings LIMIT 1"
            ) as cur:
                sample = await cur.fetchone()
            if sample and "::" not in str(sample[0]):
                print("[embeddings] Detected old single-project embedding IDs — wiping for re-index")
                await conn.execute("DROP TABLE IF EXISTS function_embeddings")
                await conn.execute("DROP TABLE IF EXISTS decision_embeddings")
                await conn.commit()
        except Exception:
            pass  # table doesn't exist yet (fresh install), or already new format

        await conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS function_embeddings USING vec0(
                id TEXT PRIMARY KEY,
                embedding FLOAT[{self._dim}]
            )
        """)
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
        for i, chunk in enumerate(needs_summary, 1):
            print(f"[embeddings]   summary {i}/{len(needs_summary)}: {chunk.id}")
            chunk.summary = await self._generate_summary(chunk)

        print(f"[embeddings] embedding {len(chunks)} functions via {self._provider}/{self._model}")
        texts = [prepare_embed_text(chunk) for chunk in chunks]
        embeddings = await self._embed_batch(texts)

        conn = self._db._db
        for chunk, embedding in zip(chunks, embeddings):
            await conn.execute(
                "UPDATE nodes SET summary = ? WHERE id = ? AND project_id = ?",
                (chunk.summary, chunk.id, project_id),
            )
            await conn.execute(
                "INSERT OR REPLACE INTO function_embeddings(id, embedding) VALUES (?, ?)",
                (_emb_id(project_id, chunk.id), _f32(embedding)),
            )
        await conn.commit()
        print(f"[embeddings] stored {len(chunks)} embeddings ok")

    async def delete_by_file(self, file_path: str, project_id: str) -> None:
        # Look up node IDs for this file+project, then delete their prefixed embeddings.
        # Must be called BEFORE CallGraphDB.delete_file_data (needs the nodes rows).
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
        prefixed = [_emb_id(project_id, fid) for fid in function_ids]
        conn = self._db._db
        ph = ",".join("?" * len(prefixed))
        await conn.execute(
            f"DELETE FROM function_embeddings WHERE id IN ({ph})", prefixed
        )
        await conn.commit()

    async def query_similar(
        self, snippet: str, top_k: int = 10, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        embedding = await self._embed_single(snippet)
        conn = self._db._db
        # Request extra candidates when project-filtering so we still get top_k results
        # after the JOIN filters out other projects.
        k_inner = top_k * 5 if project_id else top_k
        pid_filter = "AND n.project_id = ?" if project_id else ""
        async with conn.execute(
            f"""
            SELECT knn.id, knn.distance,
                   n.file, n.module, n.name, n.signature, n.summary, n.project_id
            FROM (
                SELECT id, distance
                FROM function_embeddings
                WHERE embedding MATCH ? AND k = ?
                ORDER BY distance
            ) knn
            JOIN nodes n
              ON n.id          = substr(knn.id, instr(knn.id, '::') + 2)
             AND n.project_id  = substr(knn.id, 1, instr(knn.id, '::') - 1)
            {pid_filter}
            LIMIT ?
            """,
            (_f32(embedding), k_inner, *((project_id,) if project_id else ()), top_k),
        ) as cur:
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]

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
            print(f"[embeddings]     → {summary[:80]}")
            return summary
        except Exception as exc:
            fallback = chunk.docstring[:200] if chunk.docstring else chunk.signature
            print(f"[embeddings]     summary failed ({exc}), using fallback")
            return fallback
