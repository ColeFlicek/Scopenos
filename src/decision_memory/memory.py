from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from ..call_graph.storage import CallGraphDB
from ..embeddings.embedder import EmbeddingStore


class DecisionMemory:
    """
    Layer 3 — decision reasoning storage.

    Structured records (full decision, function linkage, parent chain) live in
    SQLite. Semantic search over decision *reasoning* uses the decision_embeddings
    vec0 table managed by EmbeddingStore — same embedding model as Layer 2,
    completely separate search space.

    No external services. No secondary API calls.
    """

    def __init__(self, db: CallGraphDB, embeddings: EmbeddingStore) -> None:
        self._db = db
        self._embeddings = embeddings

    @classmethod
    async def create(cls, db: CallGraphDB, embeddings: EmbeddingStore) -> "DecisionMemory":
        # decision_embeddings vec0 table already created by EmbeddingStore.init()
        return cls(db, embeddings)

    async def close(self) -> None:
        pass

    # ── MCP tools ──────────────────────────────────────────────────────────

    async def log_decision(
        self,
        type: str,
        description: str,
        rejected_alternatives: str = "",
        trigger: str = "",
        linked_function_ids: list[str] | None = None,
        parent_decision_id: str | None = None,
    ) -> dict[str, str]:
        decision_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Structured record → SQLite
        await self._db.insert_decision({
            "id": decision_id,
            "type": type,
            "description": description,
            "rejected_alternatives": rejected_alternatives,
            "trigger": trigger,
            "parent_decision_id": parent_decision_id,
            "created_at": now,
        })
        if linked_function_ids:
            await self._db.insert_decision_functions(decision_id, linked_function_ids)

        # Reasoning embedding → sqlite-vec (via EmbeddingStore)
        reasoning = _reasoning_text(
            type, description, rejected_alternatives, trigger, linked_function_ids or []
        )
        await self._embeddings.upsert_decision_embedding(decision_id, reasoning)

        return {"decision_id": decision_id, "created_at": now}

    async def get_decision_history(self, function_name: str) -> list[dict[str, Any]]:
        """All decisions linked to a function, ordered chronologically. Pure SQLite."""
        return await self._db.get_decisions_for_function(function_name)

    async def query_decisions(self, query_text: str, top_k: int = 10) -> list[dict[str, Any]]:
        """
        Semantic search over decision reasoning.
        Finds prior thinking similar to the query — not code structure.
        """
        hits = await self._embeddings.query_decision_embeddings(query_text, top_k)
        if not hits:
            return []
        id_to_distance = {h["id"]: h["distance"] for h in hits}
        ph = ",".join("?" * len(hits))
        async with self._db._db.execute(
            f"SELECT * FROM decisions WHERE id IN ({ph})", list(id_to_distance.keys())
        ) as cur:
            rows = {r["id"]: dict(r) for r in await cur.fetchall()}
        results = []
        for hit in hits:
            rec = rows.get(hit["id"])
            if rec:
                # sqlite-vec returns L2 distance; convert to a 0–1 similarity score
                # using the known range [0, 2] for unit-normalized embeddings.
                rec["score"] = round(1.0 - hit["distance"] / 2.0, 4)
                results.append(rec)
        return results


def _reasoning_text(
    type: str,
    description: str,
    rejected_alternatives: str,
    trigger: str,
    linked_function_ids: list[str],
) -> str:
    """
    Build embed text focused on intent and reasoning — not code shape.
    This keeps Layer 3 semantically distinct from Layer 2 (which embeds
    function signatures and bodies).
    """
    parts = [
        f"Decision type: {type}",
        f"What was decided: {description}",
    ]
    if rejected_alternatives:
        parts.append(f"What was rejected: {rejected_alternatives}")
    if trigger:
        parts.append(f"What triggered this: {trigger}")
    if linked_function_ids:
        parts.append(f"Governs: {', '.join(linked_function_ids)}")
    return "\n".join(parts)
