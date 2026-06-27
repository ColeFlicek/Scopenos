from __future__ import annotations

from contextvars import ContextVar

from starlette.exceptions import HTTPException

from .call_graph.storage import CallGraphDB

_current_user: ContextVar[dict | None] = ContextVar("_current_user", default=None)
_auth_db: CallGraphDB | None = None


def set_auth_db(db: CallGraphDB) -> None:
    """Register the DB for use by AuthMiddleware. Call once during server startup."""
    global _auth_db
    _auth_db = db


def get_current_user() -> dict | None:
    """Return the authenticated user for the current request, or None."""
    return _current_user.get()


def require_user() -> dict:
    """Return the authenticated user or raise 401.

    Call from any handler (MCP tool or REST route) that must be authenticated.
    The user is set by AuthMiddleware before the handler is invoked — no DB
    call here; it's free after the per-request lookup the middleware already did.
    """
    user = _current_user.get()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user


class AuthMiddleware:
    """ASGI middleware — resolves X-API-Key to a user for every HTTP request.

    Runs once per request at the HTTP layer, before FastMCP message dispatch
    and before any REST route handler. Both MCP tools and REST routes read
    identity via get_current_user() / require_user() from the same ContextVar.
    """

    def __init__(self, app) -> None:
        self._app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {k: v for k, v in scope.get("headers", [])}
            raw_key = headers.get(b"x-api-key", b"").decode("utf-8", errors="replace")
            user = None
            if raw_key and _auth_db is not None:
                user = await _auth_db.get_user_by_key(raw_key)
            token = _current_user.set(user)
            try:
                await self._app(scope, receive, send)
            finally:
                _current_user.reset(token)
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
