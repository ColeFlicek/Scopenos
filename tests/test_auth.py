"""
Tests for auth DB methods, permission gate, and auth middleware.

Covers: create_user, create_api_key, get_user_by_key, check_project_access,
check_permission, and AuthMiddleware context propagation.

All DB tests use a real in-memory SQLite DB — no mocks on the data layer.
"""
import pytest
import pytest_asyncio
import httpx
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from src.call_graph.storage import CallGraphDB


# ── Fixtures ──────────────────────────────────────────────────────────────────

# db fixture comes from conftest.py (shared Postgres test database)


# ── create_user ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_user_returns_user_with_email(db):
    user = await db.create_user("alice@example.com")
    assert user["email"] == "alice@example.com"
    assert user["plan"] == "free"
    assert user["id"]


@pytest.mark.asyncio
async def test_create_user_duplicate_email_raises(db):
    await db.create_user("alice@example.com")
    with pytest.raises(Exception):
        await db.create_user("alice@example.com")


# ── create_api_key + get_user_by_key ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_user_by_valid_key_returns_user(db):
    user = await db.create_user("alice@example.com")
    raw_key = await db.create_api_key(user["id"], name="test key")
    found = await db.get_user_by_key(raw_key)
    assert found["id"] == user["id"]
    assert found["email"] == "alice@example.com"


@pytest.mark.asyncio
async def test_get_user_by_invalid_key_returns_none(db):
    result = await db.get_user_by_key("not-a-real-key")
    assert result is None


@pytest.mark.asyncio
async def test_get_user_by_key_updates_last_used(db):
    user = await db.create_user("bob@example.com")
    raw_key = await db.create_api_key(user["id"])
    await db.get_user_by_key(raw_key)
    async with db._db.execute(
        "SELECT last_used FROM api_keys WHERE user_id = ?", (user["id"],)
    ) as cur:
        row = await cur.fetchone()
    assert row["last_used"] is not None


# ── check_project_access ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def alice(db):
    return await db.create_user("alice@example.com")


@pytest_asyncio.fixture
async def demo(db):
    """Add pytest as a demo project."""
    from datetime import datetime, timezone
    await db._db.execute(
        "INSERT INTO demo_projects (project_id, display_name, repo_url, added_at) VALUES (?, ?, ?, ?)",
        ("pytest", "pytest", "https://github.com/pytest-dev/pytest", datetime.now(timezone.utc).isoformat()),
    )
    await db._db.commit()


@pytest.mark.asyncio
async def test_demo_project_read_allowed_for_authenticated_user(db, alice, demo):
    allowed = await db.check_project_access(alice["id"], "pytest", "read")
    assert allowed is True


@pytest.mark.asyncio
async def test_demo_project_write_denied(db, alice, demo):
    allowed = await db.check_project_access(alice["id"], "pytest", "write")
    assert allowed is False


@pytest.mark.asyncio
async def test_owner_can_read_and_write_own_project(db, alice):
    await db._db.execute(
        "INSERT INTO project_access (user_id, project_id, role) VALUES (?, ?, ?)",
        (alice["id"], "myrepo", "owner"),
    )
    await db._db.commit()
    assert await db.check_project_access(alice["id"], "myrepo", "read") is True
    assert await db.check_project_access(alice["id"], "myrepo", "write") is True


@pytest.mark.asyncio
async def test_non_owner_cannot_access_private_project(db, alice):
    bob = await db.create_user("bob@example.com")
    await db._db.execute(
        "INSERT INTO project_access (user_id, project_id, role) VALUES (?, ?, ?)",
        (alice["id"], "alices-repo", "owner"),
    )
    await db._db.commit()
    assert await db.check_project_access(bob["id"], "alices-repo", "read") is False
    assert await db.check_project_access(bob["id"], "alices-repo", "write") is False


@pytest.mark.asyncio
async def test_unknown_project_denied(db, alice):
    assert await db.check_project_access(alice["id"], "nonexistent", "read") is False


# ── _check_permission ─────────────────────────────────────────────────────────

from src.auth import check_permission


@pytest.mark.asyncio
async def test_unauthenticated_raises_401(db):
    with pytest.raises(HTTPException) as exc:
        await check_permission(None, "any-project", "read", db)
    assert exc.value.status_code == 401


@pytest.mark.asyncio
async def test_demo_project_read_passes(db, alice, demo):
    await check_permission(alice, "pytest", "read", db)  # must not raise


@pytest.mark.asyncio
async def test_demo_project_write_raises_403(db, alice, demo):
    with pytest.raises(HTTPException) as exc:
        await check_permission(alice, "pytest", "write", db)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_owner_write_passes(db, alice):
    await db._db.execute(
        "INSERT INTO project_access (user_id, project_id, role) VALUES (?, ?, ?)",
        (alice["id"], "myrepo", "owner"),
    )
    await db._db.commit()
    await check_permission(alice, "myrepo", "write", db)  # must not raise


@pytest.mark.asyncio
async def test_non_owner_raises_403(db, alice):
    bob = await db.create_user("bob@example.com")
    await db._db.execute(
        "INSERT INTO project_access (user_id, project_id, role) VALUES (?, ?, ?)",
        (alice["id"], "myrepo", "owner"),
    )
    await db._db.commit()
    with pytest.raises(HTTPException) as exc:
        await check_permission(bob, "myrepo", "read", db)
    assert exc.value.status_code == 403


# ── AuthMiddleware ─────────────────────────────────────────────────────────────

from fastmcp.server.http import _current_http_request
from fastmcp.server.middleware import MiddlewareContext
from src.auth import AuthMiddleware, get_current_user, set_auth_db


class _AuthMiddlewareAsgiAdapter:
    """Bridges FastMCP AuthMiddleware into a bare Starlette ASGI app for tests.

    Sets _current_http_request (the ContextVar that get_http_request() reads) then
    calls AuthMiddleware.on_message with the ASGI app as call_next — the exact
    same code path as production, just without a full FastMCP server.
    """

    def __init__(self, app):
        self.app = app
        self._mw = AuthMiddleware()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        http_token = _current_http_request.set(request)
        try:
            async def call_next(ctx):
                await self.app(scope, receive, send)

            await self._mw.on_message(MiddlewareContext(message=None), call_next)
        finally:
            _current_http_request.reset(http_token)


def make_test_app(db):
    """Minimal Starlette app with AuthMiddleware (via adapter) that echoes the current user."""
    set_auth_db(db)

    async def whoami(request: Request) -> JSONResponse:
        user = get_current_user()
        if user is None:
            return JSONResponse({"user": None}, status_code=200)
        return JSONResponse({"user": user["email"]}, status_code=200)

    app = Starlette(routes=[Route("/whoami", whoami)])
    app.add_middleware(_AuthMiddlewareAsgiAdapter)
    return app


@pytest.mark.asyncio
async def test_middleware_sets_user_for_valid_key(db):
    user = await db.create_user("alice@example.com")
    raw_key = await db.create_api_key(user["id"])
    app = make_test_app(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/whoami", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    assert resp.json()["user"] == "alice@example.com"


@pytest.mark.asyncio
async def test_middleware_sets_none_for_missing_key(db):
    app = make_test_app(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/whoami")
    assert resp.status_code == 200
    assert resp.json()["user"] is None


@pytest.mark.asyncio
async def test_middleware_sets_none_for_invalid_key(db):
    app = make_test_app(db)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/whoami", headers={"X-API-Key": "bogus-key"})
    assert resp.status_code == 200
    assert resp.json()["user"] is None
