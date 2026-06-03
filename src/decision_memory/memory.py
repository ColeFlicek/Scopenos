from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Any

from graphiti_core import Graphiti
from graphiti_core.nodes import EpisodeType

from ..call_graph.storage import CallGraphDB

_GROUP_DECISIONS = "decisions"


class DecisionMemory:
    """
    Layer 3: stores architectural / design / implementation decisions.
    SQLite handles structured lookups (get_decision_history by function).
    Graphiti handles semantic search (query_decisions).
    """

    def __init__(self, db: CallGraphDB, neo4j_uri: str, neo4j_user: str, neo4j_password: str) -> None:
        self._db = db
        self._graphiti = Graphiti(neo4j_uri, neo4j_user, neo4j_password)

    @classmethod
    async def create(
        cls, db: CallGraphDB, neo4j_uri: str, neo4j_user: str, neo4j_password: str
    ) -> "DecisionMemory":
        obj = cls(db, neo4j_uri, neo4j_user, neo4j_password)
        await obj.init()
        return obj

    async def init(self) -> None:
        await self._graphiti.build_indices_and_constraints()

    async def close(self) -> None:
        await self._graphiti.close()

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

        # Store structured record in SQLite
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

        # Store in Graphiti for semantic search
        episode_body = _format_decision_episode(
            decision_id=decision_id,
            type=type,
            description=description,
            rejected_alternatives=rejected_alternatives,
            trigger=trigger,
            linked_function_ids=linked_function_ids or [],
            parent_decision_id=parent_decision_id,
        )
        await self._graphiti.add_episode(
            name=decision_id,
            episode_body=episode_body,
            source=EpisodeType.text,
            source_description="decision_memory",
            reference_time=datetime.now(timezone.utc),
            group_id=_GROUP_DECISIONS,
        )

        return {"decision_id": decision_id, "created_at": now}

    async def get_decision_history(self, function_name: str) -> list[dict[str, Any]]:
        """Return all decisions linked to a function, ordered by creation time."""
        return await self._db.get_decisions_for_function(function_name)

    async def query_decisions(self, query_text: str, top_k: int = 10) -> list[dict[str, Any]]:
        """Semantic search over the decision corpus via Graphiti."""
        results = await self._graphiti.search(
            query=query_text,
            group_ids=[_GROUP_DECISIONS],
            num_results=top_k,
        )
        output = []
        for r in results:
            # r is an EntityEdge; extract readable info
            entry: dict[str, Any] = {"fact": getattr(r, "fact", ""), "uuid": getattr(r, "uuid", "")}
            output.append(entry)
        return output


def _format_decision_episode(
    decision_id: str,
    type: str,
    description: str,
    rejected_alternatives: str,
    trigger: str,
    linked_function_ids: list[str],
    parent_decision_id: str | None,
) -> str:
    lines = [
        f"Decision ID: {decision_id}",
        f"Type: {type}",
        f"Description: {description}",
    ]
    if rejected_alternatives:
        lines.append(f"Rejected alternatives: {rejected_alternatives}")
    if trigger:
        lines.append(f"Trigger: {trigger}")
    if linked_function_ids:
        lines.append(f"Linked functions: {', '.join(linked_function_ids)}")
    if parent_decision_id:
        lines.append(f"Parent decision: {parent_decision_id}")
    return "\n".join(lines)
