"""Shared helpers used across all tool group modules."""
from __future__ import annotations

from typing import TypedDict

from starlette.exceptions import HTTPException

from ..auth import get_current_user, check_permission

_USER_QUEUE_DEPTH_LIMIT = 3

_PATTERN_CONTRACT_NOTICE = (
    "PATTERN CONTRACT: If new functions are added to this subsystem, "
    "add their IDs to this contract's function_ids to maintain coverage."
)


class ChangeHint(TypedDict, total=False):
    type: str
    message: str
    suggested_id: str
    id: str
    file: str
    similarity: float
    co_change_count: int
    visitor_classes: list[str]
    missing_handlers: list[str]
    action: str


async def get_services():
    """Late-binding: resolves src.server._get_services at call time so test patches work."""
    import sys
    _srv = sys.modules.get("src.server")
    if _srv is None:
        import importlib
        _srv = importlib.import_module("src.server")
    return await _srv._get_services()


async def resolve_project_db(project_id: str, org_db):
    """Return a project-scoped CallGraphDB for reads (search_path = project schema, public).

    Falls back to org_db when project_id is empty — covers cross-project searches
    and org-level queries that don't target a specific schema.

    If the project doesn't exist in the user's org DB, falls back to the public
    demos org (org_demos registered in the control DB). This allows any authenticated
    user to query demo projects without data replication — the control DB is the
    authority on which org is public, not each individual org DB.
    """
    if not project_id:
        return org_db
    if await org_db.project_exists(project_id):
        schema_name = await org_db.get_schema_name_for_project(project_id)
        return await org_db.project_db(schema_name)
    # Not in user's org — check the public demos org
    from ..auth import get_public_org_db
    demos_db = await get_public_org_db()
    if demos_db is not None and await demos_db.project_exists(project_id):
        schema_name = await demos_db.get_schema_name_for_project(project_id)
        return await demos_db.project_db(schema_name)
    # Fall back so callers get an empty result rather than a crash
    schema_name = await org_db.get_schema_name_for_project(project_id)
    return await org_db.project_db(schema_name)


async def check_read_access(project_id: str, db) -> None:
    user = get_current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if not project_id:
        return
    try:
        await check_permission(user, project_id, "read", db)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        # Project not in user's org — check if it's accessible via the demos org.
        # The demos org is the authority on public projects; its demo_projects table
        # is seeded at provisioning time by the admin, not per-org.
        from ..auth import get_public_org_db
        demos_db = await get_public_org_db()
        if demos_db is None or not await demos_db.is_demo_project(project_id):
            raise


def fmt_contracts(contracts: list[dict]) -> list[dict]:
    out = []
    for c in contracts:
        entry = {
            "contract_id": c["id"],
            "title": c["title"],
            "rule": c["natural_language"],
            "rule_type": c["rule_type"],
            "status": c["status"],
        }
        if c.get("function_ids"):
            entry["notice"] = _PATTERN_CONTRACT_NOTICE
        out.append(entry)
    return out


async def contracts_for_name(db, function_name: str, project_id: str) -> list[dict]:
    hits = await db.find_node_by_name(function_name, project_id or None)
    if not hits:
        return []
    node = hits[0]
    return await db.get_contracts_for_function(
        node["id"], node.get("project_id") or project_id
    )


def check_and_enqueue(user_id: str, fn, *args, job_timeout: int = 3600, **kwargs):
    import time
    from rq.job import Job, JobStatus
    from .. import queue as _queue_mod

    q = _queue_mod.get_queue()
    redis = q.connection
    depth_key = f"scopenos:user_queue_depth:{user_id}"
    now = time.time()

    redis.zremrangebyscore(depth_key, "-inf", now)

    active_ids = redis.zrange(depth_key, 0, -1)
    stale = []
    for raw_id in active_ids:
        jid = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
        try:
            j = Job.fetch(jid, connection=redis)
            if j.get_status() not in (JobStatus.QUEUED, JobStatus.STARTED):
                stale.append(raw_id)
        except Exception:
            stale.append(raw_id)
    if stale:
        redis.zrem(depth_key, *stale)

    active_count = redis.zcard(depth_key)
    if active_count >= _USER_QUEUE_DEPTH_LIMIT:
        raise RuntimeError("rate_limited")

    job = q.enqueue(fn, *args, job_timeout=job_timeout, **kwargs)
    expire_at = now + job_timeout
    redis.zadd(depth_key, {job.id: expire_at})
    redis.expireat(depth_key, int(expire_at) + 60)
    return job
