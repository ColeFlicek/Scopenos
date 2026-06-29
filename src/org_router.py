"""Org-level request routing: API key → org_id → org CallGraphDB pool.

OrgRouter holds the control-plane DB for key lookup and lazily creates
per-org connection pools from the db_url stored in the organizations table.

Every API key must have an org_id. Keys without an org_id are rejected with
403. There is no single-tenant fallback — every deployment uses the same
structure: CONTROL_DB_URL (control plane) + per-org databases.

CONTROL_DB_URL is required at startup. The server will not start without it.
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

        CONTROL_DB_URL is required. Raises RuntimeError at startup if unset.
        """
        from .call_graph.storage import CallGraphDB
        dsn = os.getenv("CONTROL_DB_URL")
        if not dsn:
            raise RuntimeError(
                "CONTROL_DB_URL is required but not set. "
                "Point it at the Scopenos control plane database."
            )
        control_db = await CallGraphDB.create(dsn, schema="scopenos", skip_schema_init=True)
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
        from starlette.exceptions import HTTPException

        if org_id is None:
            raise HTTPException(
                403,
                "API key is not scoped to an org. "
                "Re-issue the key via /api/signup or create_user.py --org-id <slug>.",
            )

        if org_id in self._pools:
            return self._pools[org_id]

        async with self._lock:
            if org_id in self._pools:
                return self._pools[org_id]

            db_url = await self._control_db.get_org_db_url(org_id)
            if not db_url:
                raise HTTPException(
                    503,
                    f"No database registered for org '{org_id}'. "
                    "Run provision_org.py to set up the org database.",
                )

            from .call_graph.storage import CallGraphDB
            org_db = await CallGraphDB.create(db_url, skip_schema_init=True)
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
