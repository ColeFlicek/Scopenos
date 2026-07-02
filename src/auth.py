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

    async def resolve_request(self, raw_key: str, endpoint: str = "") -> tuple[dict | None, CallGraphDB]:
        user = await self._db.get_user_by_key(raw_key)
        return user, self._db

    async def log_no_key_event(self, endpoint: str = "") -> None:
        pass


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
    """Return the authenticated user or raise 401."""
    user = _current_user.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


def require_admin() -> dict:
    """Return the authenticated user or raise 401/403. User must have is_admin=True."""
    user = _current_user.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin key required")
    return user


def get_control_db() -> "CallGraphDB | None":
    """Return the control-plane CallGraphDB if the router is initialized."""
    if _org_router is None:
        return None
    return getattr(_org_router, "control_db", None)


async def get_public_org_db() -> "CallGraphDB | None":
    """Return the public demos org DB (org_demos), or None if not provisioned.

    The demos org is registered in the control DB's organizations table with
    slug='demos'. Any org with that slug acts as the public read layer —
    projects indexed there are readable by any authenticated user.
    """
    if _org_router is None:
        return None
    try:
        return await _org_router._get_org_db("demos")
    except Exception:
        return None


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
            endpoint = scope.get("path", "")
            user = None
            org_db = None
            if raw_key and _org_router is not None:
                try:
                    user, org_db = await _org_router.resolve_request(raw_key, endpoint=endpoint)
                except Exception as _exc:
                    import traceback
                    print(f"[AuthMiddleware] resolve_request failed: {type(_exc).__name__}: {_exc!r}\n{traceback.format_exc()}", flush=True)
                    # Leave user=None, org_db=None — downstream will raise 401/503
            elif _org_router is not None and not raw_key:
                import asyncio as _asyncio
                _asyncio.create_task(
                    _org_router.log_no_key_event(endpoint)
                )
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
