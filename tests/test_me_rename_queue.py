"""
Tests for:
  - CallGraphDB.rename_project / list_user_projects
  - GET /api/me
  - PATCH /api/projects/{project_id}
  - _check_and_enqueue queue depth limiting
"""
import pytest
import pytest_asyncio
import httpx
from datetime import datetime, timezone

from src.call_graph.storage import CallGraphDB
from src.auth import set_auth_db


# ── App factory ────────────────────────────────────────────────────────────────

def make_app(db, email_sender=None):
    """Minimal ASGI app with routes under test (me, rename, signup)."""
    from fastmcp import FastMCP
    from src.web.routes import register_routes

    async def get_services():
        class _Svc:
            pass
        svc = _Svc()
        svc.db = db
        return svc

    mcp = FastMCP("test")
    register_routes(mcp, get_services, email_sender=email_sender or (lambda *_: None))
    return mcp.http_app()


async def _get(app, path, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(path, headers=headers or {})


async def _patch(app, path, json, headers=None):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.patch(path, json=json, headers=headers or {})


@pytest_asyncio.fixture(autouse=True)
async def _wire_auth(db):
    set_auth_db(db)


# ── rename_project (storage) ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rename_project_returns_true_and_updates_name(db):
    await db.upsert_project("myapp", "myapp", "/tmp/myapp")

    result = await db.rename_project("myapp", "My Application")

    assert result is True
    projects = await db.list_projects()
    names = {p["id"]: p["name"] for p in projects}
    assert names["myapp"] == "My Application"


@pytest.mark.asyncio
async def test_rename_project_returns_false_for_unknown_id(db):
    result = await db.rename_project("does-not-exist", "Anything")
    assert result is False


# ── list_user_projects (storage) ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_user_projects_includes_owned_project(db):
    user = await db.create_user("alice@example.com")
    await db.upsert_project("proj1", "Project One", "/tmp/proj1")
    await db._db.execute(
        "INSERT INTO project_access (user_id, project_id, role) VALUES (?, ?, ?)",
        (user["id"], "proj1", "owner"),
    )
    await db._db.commit()

    projects = await db.list_user_projects(user["id"])

    assert any(p["id"] == "proj1" and p["role"] == "owner" for p in projects)


@pytest.mark.asyncio
async def test_list_user_projects_includes_demo_projects(db):
    user = await db.create_user("bob@example.com")
    now = datetime.now(timezone.utc).isoformat()
    await db.upsert_project("demo-repo", "Demo Repo", "")
    await db._db.execute(
        "INSERT INTO demo_projects (project_id, display_name, repo_url, added_at) VALUES (?, ?, ?, ?)",
        ("demo-repo", "Demo Repo", "https://github.com/example/demo", now),
    )
    await db._db.commit()

    projects = await db.list_user_projects(user["id"])

    assert any(p["id"] == "demo-repo" for p in projects)


@pytest.mark.asyncio
async def test_list_user_projects_demo_role_is_viewer(db):
    user = await db.create_user("carol@example.com")
    now = datetime.now(timezone.utc).isoformat()
    await db.upsert_project("demo2", "Demo 2", "")
    await db._db.execute(
        "INSERT INTO demo_projects (project_id, display_name, repo_url, added_at) VALUES (?, ?, ?, ?)",
        ("demo2", "Demo 2", "https://github.com/example/demo2", now),
    )
    await db._db.commit()

    projects = await db.list_user_projects(user["id"])
    demo = next(p for p in projects if p["id"] == "demo2")

    assert demo["role"] == "viewer"


# ── GET /api/me ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_me_without_key_returns_401(db):
    app = make_app(db)
    resp = await _get(app, "/api/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_invalid_key_returns_401(db):
    app = make_app(db)
    resp = await _get(app, "/api/me", headers={"X-API-Key": "not-a-real-key"})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_with_valid_key_returns_user_and_projects(db):
    user = await db.create_user("dave@example.com")
    raw_key = await db.create_api_key(user["id"])
    app = make_app(db)

    resp = await _get(app, "/api/me", headers={"X-API-Key": raw_key})

    assert resp.status_code == 200
    body = resp.json()
    assert body["user"]["email"] == "dave@example.com"
    assert "projects" in body


@pytest.mark.asyncio
async def test_me_projects_list_includes_owned_project(db):
    user = await db.create_user("eve@example.com")
    raw_key = await db.create_api_key(user["id"])
    await db.upsert_project("eve-proj", "Eve Project", "/tmp/eve")
    await db._db.execute(
        "INSERT INTO project_access (user_id, project_id, role) VALUES (?, ?, ?)",
        (user["id"], "eve-proj", "owner"),
    )
    await db._db.commit()
    app = make_app(db)

    resp = await _get(app, "/api/me", headers={"X-API-Key": raw_key})

    body = resp.json()
    assert any(p["id"] == "eve-proj" for p in body["projects"])


# ── PATCH /api/projects/{project_id} ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_rename_endpoint_missing_name_returns_400(db):
    await db.upsert_project("app1", "App One", "/tmp/app1")
    app = make_app(db)
    resp = await _patch(app, "/api/projects/app1", json={})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_rename_endpoint_unknown_project_returns_404(db):
    app = make_app(db)
    resp = await _patch(app, "/api/projects/ghost", json={"name": "Ghost"})
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rename_endpoint_updates_name_and_returns_200(db):
    await db.upsert_project("app2", "App Two", "/tmp/app2")
    app = make_app(db)

    resp = await _patch(app, "/api/projects/app2", json={"name": "New Name"})

    assert resp.status_code == 200
    assert resp.json()["name"] == "New Name"
    projects = await db.list_projects()
    assert any(p["id"] == "app2" and p["name"] == "New Name" for p in projects)


# ── Queue depth limiting (_check_and_enqueue) ──────────────────────────────────

@pytest.mark.asyncio
async def test_queue_depth_allows_jobs_under_limit(monkeypatch):
    import fakeredis
    from rq import Queue
    from unittest.mock import MagicMock
    import src.server as server_mod

    fake_redis = fakeredis.FakeRedis()
    fake_queue = Queue(connection=fake_redis)

    def fake_get_queue():
        return fake_queue

    monkeypatch.setattr(server_mod._queue_mod, "get_queue", fake_get_queue)

    def noop(*args, **kwargs):
        pass

    job = server_mod._check_and_enqueue("user-1", noop, job_timeout=60)
    assert job is not None
    assert fake_redis.zcard("phronosis:user_queue_depth:user-1") == 1


@pytest.mark.asyncio
async def test_queue_depth_blocks_at_limit(monkeypatch):
    import fakeredis
    from rq import Queue
    from rq.job import JobStatus
    from unittest.mock import patch, MagicMock
    import src.server as server_mod

    fake_redis = fakeredis.FakeRedis()
    fake_queue = Queue(connection=fake_redis)

    def fake_get_queue():
        return fake_queue

    monkeypatch.setattr(server_mod._queue_mod, "get_queue", fake_get_queue)

    def noop(*args, **kwargs):
        pass

    # Fill up to the limit
    for _ in range(server_mod._USER_QUEUE_DEPTH_LIMIT):
        server_mod._check_and_enqueue("user-2", noop, job_timeout=3600)

    with pytest.raises(RuntimeError):
        server_mod._check_and_enqueue("user-2", noop, job_timeout=3600)


@pytest.mark.asyncio
async def test_queue_depth_does_not_count_finished_jobs(monkeypatch):
    import fakeredis
    from rq import Queue
    from rq.job import JobStatus
    import src.server as server_mod

    fake_redis = fakeredis.FakeRedis()
    fake_queue = Queue(connection=fake_redis)

    def fake_get_queue():
        return fake_queue

    monkeypatch.setattr(server_mod._queue_mod, "get_queue", fake_get_queue)

    def noop(*args, **kwargs):
        pass

    # Enqueue up to limit, then manually finish all jobs
    jobs = []
    for _ in range(server_mod._USER_QUEUE_DEPTH_LIMIT):
        j = server_mod._check_and_enqueue("user-3", noop, job_timeout=3600)
        jobs.append(j)

    # Mark all as finished in RQ's registry
    for j in jobs:
        j.set_status(JobStatus.FINISHED)

    # Now a new job should succeed
    new_job = server_mod._check_and_enqueue("user-3", noop, job_timeout=3600)
    assert new_job is not None
