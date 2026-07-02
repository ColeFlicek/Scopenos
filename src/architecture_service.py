from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .analysis import ArchitectureAnalyzer
from .call_graph.storage import CallGraphDB

if TYPE_CHECKING:
    from .call_graph.storage import GraphData


class ArchitectureService:
    """
    Coordinator for the architectural intelligence pipeline.

    Owns three concerns that do not belong in the storage layer:
    - running ArchitectureAnalyzer over raw graph data
    - persisting the snapshot (via CallGraphDB.save_project_snapshot)
    - in-memory TTL cache so repeated calls within the same process
      don't re-run 8 SQL queries and the analyzer on every request

    CallGraphDB.fetch_graph_data provides the raw SQL bundle.
    ArchitectureAnalyzer.snapshot transforms it.
    This class orchestrates.

    Attach one instance per cached CallGraphDB (pdb._arch_service) so
    the cache survives across requests. Creating a new instance per request
    defeats the cache — it starts empty every time.
    """

    def __init__(self, db: CallGraphDB) -> None:
        self._db = db
        self._snapshot_cache: dict[str, tuple[float, dict]] = {}
        self._data_cache: dict[str, tuple[float, "GraphData"]] = {}

    async def get_graph_data(
        self, project_id: str, max_age_seconds: int = 300
    ) -> "GraphData":
        """Return raw GraphData, served from cache if fresh enough."""
        if max_age_seconds > 0:
            cached = self._data_cache.get(project_id)
            if cached and (time.monotonic() - cached[0]) < max_age_seconds:
                return cached[1]
        data = await self._db.fetch_graph_data(project_id)
        self._data_cache[project_id] = (time.monotonic(), data)
        return data

    def invalidate(self, project_id: str) -> None:
        """Drop cached data for project_id (call after index_changes)."""
        self._snapshot_cache.pop(project_id, None)
        self._data_cache.pop(project_id, None)

    async def get_project_home(
        self, project_id: str, max_age_seconds: int = 0
    ) -> dict:
        """
        Return a full architectural snapshot for project_id.

        max_age_seconds: if > 0 and a cached result is younger than this,
        return it without re-running queries and the analyzer. 0 always
        recomputes.
        """
        if max_age_seconds > 0:
            cached = self._snapshot_cache.get(project_id)
            if cached and (time.monotonic() - cached[0]) < max_age_seconds:
                return cached[1]

        data = await self.get_graph_data(project_id, max_age_seconds=max_age_seconds)
        snapshot = ArchitectureAnalyzer().snapshot(data)
        result = dataclasses.asdict(snapshot)

        now_iso = datetime.now(timezone.utc).isoformat()
        await self._db.save_project_snapshot(project_id, data.current_hashes, now_iso)

        self._snapshot_cache[project_id] = (time.monotonic(), result)
        return result
