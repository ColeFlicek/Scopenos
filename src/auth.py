from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING

from starlette.exceptions import HTTPException

from .call_graph.storage import CallGraphDB

if TYPE_CHECKING:
    pass

_current_user: ContextVar[dict | None] = ContextVar("_current_user", default=None)
_current_org_db: ContextVar[CallGraphDB | None] = ContextVar("_current_org_db", default=None)
_org_router = None  # OrgRouter | _SimpleRouter | None


class _SimpleRouter:
    """Passthrough router — resolves keys against one fixed DB.

    Used by set_auth_db() for backward compatibility in tests and simple
    single-process setups that don't need OrgRouter.
    """

    def __init__(self, db: CallGraphDB) -> None:
        self._db = db

    async def resolve_request(self, raw_key: str) -> tuple[dict | None, CallGraphDB]:
        user = await self._db.get_user_by_key(raw_key)
        return user, self._db


def set_auth_db(db: CallGraphDB) -> None:
    """Backward-compat shim — wraps db in a single-DB passthrough router.

    Tests and simple setups call this. Production startup calls set_org_router().
    """
    global _org_router
    _org_router = _SimpleRouter(db)


def set_org_router(router) -> None:
    """Register the OrgRouter instance for use by AuthMiddleware."""
    global _org_router
    _org_router = router


def get_current_user() -> dict | None:
    """Return the authenticated user for the current request, or None."""
    return _current_user.get()


def get_current_org_db() -> CallGraphDB | None:
    """Return the org-scoped CallGraphDB for the current request, or None."""
    return _current_org_db.get()


def require_user() -> dict:
    """Return the authenticated user or raise 401.

    The user is set by AuthMiddleware before the handler is invoked — no DB
    call here; it's free after the per-request lookup the middleware already did.
    """
    user = _current_user.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


class AuthMiddleware:
    """ASGI middleware — resolves X-API-Key to a user + org DB for every HTTP request.

    Runs once per request at the HTTP layer, before FastMCP message dispatch
    and before any REST route handler. Both MCP tools and REST routes read
    identity via get_current_user() / require_user() from the same ContextVar.
    The org-scoped DB is available via get_current_org_db().
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {k: v for k, v in scope.get("headers", [])}
            raw_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace")
            user = None
            org_db = None
            if raw_key and _org_router is not None:
                user, org_db = await _org_router.resolve_request(raw_key)
            token_user = _current_user.set(user)
            token_db = _current_org_db.set(org_db)
            try:
                await self._app(scope, receive, send)
            finally:
                _current_user.reset(token_user)
                _current_org_db.reset(token_db)
        else:
            await self._app(scope, receive, send)


async def check_permission(
    user: dict | None,
    project_id: str,
    operation: str,
    db: CallGraphDB,
) -> None:
    """Raise HTTPException if user may not perform operation on project_id.

    operation: "read" | "write"
    Raises 401 if user is None (unauthenticated).
    Raises 403 if authenticated but not permitted.
    Returns None on success.
    """
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    allowed = await db.check_project_access(user["id"], project_id, operation)
    if not allowed:
        raise HTTPException(status_code=403, detail="Access denied")
