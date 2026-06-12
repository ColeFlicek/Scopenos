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
    """Read embedding provider, model, dimension, and Ollama URL from config file then env."""
    try:
        from ..web.config_store import read_file_config
        file_cfg = read_file_config()
    except Exception:
        file_cfg = {}

    def _get(key: str, default: str = "") -> str:
        """Look up key in file config, then env; treat empty strings as unset."""
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
        """Initialize with a CallGraphDB reference and resolve embedding configuration."""
        self._db = db
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        self._provider, self._model, self._dim, ollama_url = _resolve_config()
        self._embed_client = _make_embed_client(self._provider, ollama_url)
        # Always OpenAI for large-model fallback — used for undocumented functions
        self._large_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        self._large_model = "text-embedding-3-large"
        print(f"[embeddings] provider={self._provider} model={self._model} dim={self._dim}")

    @classmethod
    async def create(cls, db) -> "EmbeddingStore":
        """Async factory — create and fully initialize an EmbeddingStore instance."""
        obj = cls(db)
        await obj.init()
        return obj

    async def init(self) -> None:
        """Load sqlite-vec extension, run migrations, and ensure DDL is applied."""
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
                await conn.execute(f"DELETE FROM {table} WHERE id = ?", (node_id,))
                await conn.execute(
                    f"INSERT INTO {table}(id, embedding) VALUES (?, ?)",
                    (node_id, emb_blob),
                )

        await conn.execute("DROP TABLE function_embeddings")
        await conn.commit()
        print(f"[embeddings] Migrated {len(rows) - skipped} vectors across "
              f"{len(by_project)} projects ({skipped} stale entries dropped)")

    async def _ensure_emb_table(self, conn, project_id: str) -> str:
        """Create the per-project vec0 embedding table if absent; return its name."""
        table = _emb_table(project_id)
        await conn.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
                id TEXT PRIMARY KEY,
                embedding FLOAT[{self._dim}]
            )
        """)
        return table

    async def close(self) -> None:
        """No-op — the connection is owned and closed by CallGraphDB."""
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
    ) -> dict:
        """Two-tier embedding: documented functions use the configured small model;
        undocumented functions fall back to text-embedding-3-large.
        Returns {"docs": N, "fallback": N}."""
        if not chunks:
            return {"docs": 0, "fallback": 0}

        # Apply cached summaries and docstring-as-summary.
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

        # Tier 1: has summary, docstring, or leading comment → small model.
        # Tier 2: nothing → large model for better code-query alignment.
        doc_chunks = [c for c in chunks if c.summary or c.docstring or c.leading_comment]
        raw_chunks = [c for c in chunks if not (c.summary or c.docstring or c.leading_comment)]

        print(f"[embeddings] two-tier: {len(doc_chunks)} documented ({self._model}), "
              f"{len(raw_chunks)} undocumented ({self._large_model})")

        conn = self._db._db
        table = await self._ensure_emb_table(conn, project_id)

        if doc_chunks:
            texts = [prepare_embed_text(c) for c in doc_chunks]
            embeddings = await self._embed_batch(texts)
            for chunk, emb in zip(doc_chunks, embeddings):
                await conn.execute(
                    "UPDATE nodes SET summary = ?, embedding_model = ? WHERE id = ? AND project_id = ?",
                    (chunk.summary, self._model, chunk.id, project_id),
                )
                await conn.execute(f"DELETE FROM {table} WHERE id = ?", (chunk.id,))
                await conn.execute(
                    f"INSERT INTO {table}(id, embedding) VALUES (?, ?)",
                    (chunk.id, _f32(emb)),
                )

        if raw_chunks:
            texts = [prepare_embed_text(c) for c in raw_chunks]
            embeddings = await self._embed_batch_large(texts)
            for chunk, emb in zip(raw_chunks, embeddings):
                await conn.execute(
                    "UPDATE nodes SET embedding_model = ? WHERE id = ? AND project_id = ?",
                    (self._large_model, chunk.id, project_id),
                )
                await conn.execute(f"DELETE FROM {table} WHERE id = ?", (chunk.id,))
                await conn.execute(
                    f"INSERT INTO {table}(id, embedding) VALUES (?, ?)",
                    (chunk.id, _f32(emb)),
                )

        await conn.commit()
        print(f"[embeddings] stored {len(chunks)} embeddings ok "
              f"({len(doc_chunks)} small-model, {len(raw_chunks)} large-model)")

        return {"docs": len(doc_chunks), "fallback": len(raw_chunks)}

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
        """Remove embedding vectors for specific function IDs from a project's vec0 table."""
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

    async def get_embedded_ids(self, project_id: str) -> set:
        """Return the set of function IDs that currently have an embedding vector."""
        table = _emb_table(project_id)
        conn = self._db._db
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ) as cur:
            if not await cur.fetchone():
                return set()
        async with conn.execute(f"SELECT id FROM {table}") as cur:
            return {row[0] for row in await cur.fetchall()}


    async def enrich_summaries(self, project_id: str, limit: int = 500) -> dict:
        """LLM-summarize functions embedded via the large-model fallback, then re-embed with the
        configured model. User-initiated only — never called automatically during indexing."""
        conn = self._db._db
        async with conn.execute(
            "SELECT id, name, signature, docstring, file FROM nodes "
            "WHERE project_id = ? AND embedding_model = 'text-embedding-3-large' LIMIT ?",
            (project_id, limit),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return {"enriched": 0, "remaining": 0,
                    "message": "No functions need enrichment — all are already on the configured model."}

        from .chunker import FunctionChunk
        chunks = [
            FunctionChunk(
                id=row[0], name=row[1], signature=row[2], docstring=row[3] or "",
                leading_comment="", summary="", file=row[4], module="",
                type="function", body="", embed_text="",
            )
            for row in rows
        ]

        sem = asyncio.Semaphore(SUMMARY_CONCURRENCY)

        async def _summarize(chunk: FunctionChunk) -> str:
            async with sem:
                return await self._generate_summary(chunk)

        print(f"[embeddings] enriching {len(chunks)} functions with LLM summaries")
        summaries = await asyncio.gather(*[_summarize(c) for c in chunks])
        for chunk, summary in zip(chunks, summaries):
            chunk.summary = summary

        table = await self._ensure_emb_table(conn, project_id)
        texts = [prepare_embed_text(c) for c in chunks]
        embeddings = await self._embed_batch(texts)

        for chunk, emb in zip(chunks, embeddings):
            await conn.execute(
                "UPDATE nodes SET summary = ?, embedding_model = ? WHERE id = ? AND project_id = ?",
                (chunk.summary, self._model, chunk.id, project_id),
            )
            await conn.execute(f"DELETE FROM {table} WHERE id = ?", (chunk.id,))
            await conn.execute(
                f"INSERT INTO {table}(id, embedding) VALUES (?, ?)",
                (chunk.id, _f32(emb)),
            )
        await conn.commit()

        async with conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE project_id = ? AND embedding_model = 'text-embedding-3-large'",
            (project_id,),
        ) as cur:
            remaining = (await cur.fetchone())[0]

        return {
            "enriched": len(chunks),
            "remaining": remaining,
            "message": (
                f"Enriched {len(chunks)} functions with LLM summaries and re-embedded with {self._model}. "
                + (f"{remaining} still use large-model fallback — call enrich_summaries again to continue."
                   if remaining else "All functions are now on the configured model.")
            ),
        }


    async def query_similar(
        self, snippet: str, top_k: int = 10, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        """KNN search returning the top-k functions semantically similar to a code snippet."""
        embedding = await self._embed_single(snippet)
        conn = self._db._db

        if project_id:
            # Single-project query: scan only that project's table.
            # Use LOWER() comparison — sqlite_master name lookups are case-sensitive
            # but project_ids may differ in case from when the table was created.
            table_candidate = _emb_table(project_id)
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND LOWER(name)=LOWER(?)",
                (table_candidate,)
            ) as cur:
                row = await cur.fetchone()
                if not row:
                    return []
                table = row[0]  # use the actual canonical name from the DB
            async with conn.execute(
                f"""
                SELECT knn.id, knn.distance,
                       n.file, n.module, n.name, n.signature, n.summary, n.project_id
                FROM (
                    SELECT id, distance FROM "{table}"
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
                    "SELECT name FROM sqlite_master WHERE type='table' AND LOWER(name)=LOWER(?)",
                    (t,)
                ) as cur:
                    row = await cur.fetchone()
                    if row:
                        valid.append((pid, row[0]))  # use canonical name from DB

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
        """Count embedded functions for a project, or across all projects if unscoped."""
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
        await conn.execute("DELETE FROM decision_embeddings WHERE id = ?", (decision_id,))
        await conn.execute(
            "INSERT INTO decision_embeddings(id, embedding) VALUES (?, ?)",
            (decision_id, _f32(embedding)),
        )
        await conn.commit()

    async def query_decision_embeddings(
        self, query_text: str, top_k: int = 10
    ) -> list[dict[str, Any]]:
        """KNN search over decision reasoning embeddings; returns id and distance."""
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
        """Remove a decision's embedding from the decision_embeddings table."""
        conn = self._db._db
        await conn.execute(
            "DELETE FROM decision_embeddings WHERE id = ?", (decision_id,)
        )
        await conn.commit()

    # ── Layer 4: contract embeddings ───────────────────────────────────────

    def _contract_table(self, contract_id: str, kind: str) -> str:
        """Return the vec0 table name for a contract's violation or compliance examples."""
        safe = re.sub(r"[^a-zA-Z0-9]", "_", contract_id)
        return f"contract_{kind}_{safe}"

    async def _ensure_contract_tables(self, conn, contract_id: str) -> tuple[str, str]:
        """Create vec0 violation and compliance tables for a contract; return their names."""
        viol_table = self._contract_table(contract_id, "violation")
        comp_table = self._contract_table(contract_id, "compliance")
        for table in (viol_table, comp_table):
            await conn.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0(
                    id TEXT PRIMARY KEY,
                    embedding FLOAT[{self._dim}]
                )
            """)
        return viol_table, comp_table

    async def upsert_contract_embeddings(
        self,
        contract_id: str,
        violation_codes: list[str],
        compliance_codes: list[str],
    ) -> None:
        """Embed violation/compliance code examples and store them in per-contract vec0 tables."""
        import uuid
        conn = self._db._db
        viol_table, comp_table = await self._ensure_contract_tables(conn, contract_id)

        # Clear existing embeddings for this contract.
        await conn.execute(f"DELETE FROM {viol_table}")
        await conn.execute(f"DELETE FROM {comp_table}")

        all_codes = violation_codes + compliance_codes
        if not all_codes:
            await conn.commit()
            return

        embeddings = await self._embed_batch(all_codes)
        viol_embs = embeddings[:len(violation_codes)]
        comp_embs = embeddings[len(violation_codes):]

        for emb in viol_embs:
            await conn.execute(
                f"INSERT INTO {viol_table}(id, embedding) VALUES (?, ?)",
                (str(uuid.uuid4()), _f32(emb)),
            )
        for emb in comp_embs:
            await conn.execute(
                f"INSERT INTO {comp_table}(id, embedding) VALUES (?, ?)",
                (str(uuid.uuid4()), _f32(emb)),
            )
        await conn.commit()

    async def delete_contract_embeddings(self, contract_id: str) -> None:
        """Drop the vec0 violation and compliance tables for a deleted contract."""
        conn = self._db._db
        for kind in ("violation", "compliance"):
            table = self._contract_table(contract_id, kind)
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                if await cur.fetchone():
                    await conn.execute(f"DROP TABLE {table}")
        await conn.commit()

    async def check_semantic(
        self, contract_id: str, code_snippet: str
    ) -> tuple[bool, float, float]:
        """
        Check whether code_snippet violates a contract semantically.
        Returns (is_violation, violation_score, compliance_score).
        Flags as violation if violation_score >= threshold AND compliance_score < threshold.
        """
        conn = self._db._db
        viol_table = self._contract_table(contract_id, "violation")
        comp_table = self._contract_table(contract_id, "compliance")

        for table in (viol_table, comp_table):
            async with conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                if not await cur.fetchone():
                    return False, 0.0, 0.0

        async with conn.execute(
            f"SELECT COUNT(*) FROM {viol_table}"
        ) as cur:
            if (await cur.fetchone())[0] == 0:
                return False, 0.0, 0.0

        embedding = await self._embed_single(code_snippet)
        emb_bytes = _f32(embedding)

        # Best similarity against violation cluster (nearest neighbour).
        async with conn.execute(
            f"SELECT distance FROM {viol_table} WHERE embedding MATCH ? AND k = 1",
            (emb_bytes,),
        ) as cur:
            row = await cur.fetchone()
        viol_score = round(1.0 - row[0] / 2.0, 4) if row else 0.0

        # Best similarity against compliance cluster.
        # Table existence already confirmed above; just check row count.
        comp_score = 0.0
        async with conn.execute(f"SELECT COUNT(*) FROM {comp_table}") as cur:
            comp_count = (await cur.fetchone())[0]
        if comp_count > 0:
            async with conn.execute(
                f"SELECT distance FROM {comp_table} WHERE embedding MATCH ? AND k = 1",
                (emb_bytes,),
            ) as cur:
                row = await cur.fetchone()
            comp_score = round(1.0 - row[0] / 2.0, 4) if row else 0.0

        # Retrieve contract threshold.
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
        resp = await self._embed_client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

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
