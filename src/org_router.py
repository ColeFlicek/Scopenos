"""Org-level request routing: API key → org_id → org CallGraphDB pool.

OrgRouter holds the control-plane DB for key lookup and lazily creates
per-org connection pools from the db_url stored in the organizations table.

In single-tenant mode (no CONTROL_DB_URL, no org_id on API keys):
  - Control DB == DATABASE_URL
  - All requests resolve to org_id=None and route to the same pool
  - No per-org pool is ever created

In multi-tenant mode (separate org databases):
  - CONTROL_DB_URL points to the scopenos_control database
  - api_keys.org_id maps keys to orgs
  - Each org gets a dedicated pool from organizations.db_url
"""
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB


class OrgRouter:
    """Routes API key requests to the correct org database pool.

    One instance lives for the server's lifetime, created in lifespan().
    Thread-safe: pool creation is guarded by an asyncio.Lock.
    """

    def __init__(self, control_db: "CallGraphDB") -> None:
        self._control_db = control_db
        self._pools: dict[str, "CallGraphDB"] = {}
        self._lock = asyncio.Lock()

    @classmethod
    async def create(cls) -> "OrgRouter":
        """Async factory — connects to the control plane DB.

        Uses CONTROL_DB_URL if set; falls back to DATABASE_URL for
        single-tenant deployments where control and org data share one DB.
        """
        from .call_graph.storage import CallGraphDB
        dsn = os.getenv("CONTROL_DB_URL") or os.getenv(
            "DATABASE_URL", "postgresql://scopenos:scopenos@localhost/scopenos"
        )
        control_db = await CallGraphDB.create(dsn)
        return cls(control_db)

    async def resolve_request(self, raw_key: str) -> tuple[dict | None, "CallGraphDB"]:
        """Resolve a raw API key to (user, org_db).

        Returns (None, control_db) for invalid keys so the request still has
        a DB in the ContextVar — downstream permission checks will raise 401.
        """
        user = await self._control_db.get_user_by_key(raw_key)
        if user is None:
            return None, self._control_db
        org_id: str | None = user.get("org_id")
        org_db = await self._get_org_db(org_id)
        return user, org_db

    async def _get_org_db(self, org_id: str | None) -> "CallGraphDB":
        if org_id is None:
            return self._control_db

        if org_id in self._pools:
            return self._pools[org_id]

        async with self._lock:
            if org_id in self._pools:
                return self._pools[org_id]

            db_url = await self._control_db.get_org_db_url(org_id)
            if not db_url:
                # No dedicated DB registered — share the control pool.
                self._pools[org_id] = self._control_db
                return self._control_db

            from .call_graph.storage import CallGraphDB
            org_db = await CallGraphDB.create(db_url)
            self._pools[org_id] = org_db
            return org_db

    @property
    def control_db(self) -> "CallGraphDB":
        return self._control_db

    async def close(self) -> None:
        """Close all per-org pools (not the control DB — caller closes that separately)."""
        for pool in list(self._pools.values()):
            if pool is not self._control_db:
                await pool.close()
        self._pools.clear()
