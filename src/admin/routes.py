"""Admin HTTP routes — control-plane observability for the system operator.

All routes require an API key with is_admin=True.
No org data is ever returned — only control-plane metadata.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from ..auth import require_admin, get_control_db
from ..infra_smoke import run_checks, check_metadata_accuracy, CheckResult

_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


def _control_db():
    db = get_control_db()
    if db is None:
        raise HTTPException(503, "Control DB not available")
    return db


def register(mcp) -> None:
    """Register all /admin/* routes on the given FastMCP instance."""

    @mcp.custom_route("/admin/", methods=["GET"])
    async def admin_dashboard(request: Request) -> HTMLResponse:
        """Serve the admin dashboard HTML page (no auth required — page handles it client-side)."""
        html = _DASHBOARD_HTML.read_text(encoding="utf-8")
        return HTMLResponse(html)

    @mcp.custom_route("/admin/api/summary", methods=["GET"])
    async def admin_summary(request: Request) -> JSONResponse:
        """Control-plane counts: active/revoked keys, orgs, users, recent auth failures."""
        require_admin()
        db = _control_db()
        summary = await db.admin_get_summary()
        return JSONResponse(summary)

    @mcp.custom_route("/admin/api/keys", methods=["GET"])
    async def admin_keys(request: Request) -> JSONResponse:
        """All API keys with owner info and last-used timestamp. Raw values never returned."""
        require_admin()
        db = _control_db()
        keys = await db.admin_list_keys()
        return JSONResponse({"keys": keys})

    @mcp.custom_route("/admin/api/orgs", methods=["GET"])
    async def admin_orgs(request: Request) -> JSONResponse:
        """All orgs with member count."""
        require_admin()
        db = _control_db()
        orgs = await db.admin_list_orgs()
        return JSONResponse({"orgs": orgs})

    @mcp.custom_route("/admin/api/auth-log", methods=["GET"])
    async def admin_auth_log(request: Request) -> JSONResponse:
        """Recent auth events (newest first). Default 200, max 5000."""
        require_admin()
        db = _control_db()
        try:
            limit = min(int(request.query_params.get("limit", 200)), 5000)
        except (ValueError, TypeError):
            limit = 200
        events = await db.admin_get_auth_log(limit)
        return JSONResponse({"events": events})

    @mcp.custom_route("/admin/api/smoke", methods=["GET"])
    async def admin_smoke(request: Request) -> JSONResponse:
        """Run infrastructure smoke checks and return pass/fail results."""
        require_admin()
        db = _control_db()
        env_key = os.environ.get("SCOPENOS_API_KEY") or None

        results: list[CheckResult] = []
        try:
            async with db.acquire() as conn:
                results = await run_checks(conn, env_key=env_key)
        except Exception as exc:
            results = [CheckResult("connection", "ERROR", str(exc))]

        from ..auth import _org_router
        if _org_router:
            for org_id, org_db in getattr(_org_router, "_pools", {}).items():
                try:
                    async with org_db.acquire() as org_conn:
                        r = await check_metadata_accuracy(org_conn)
                        results.append(CheckResult(
                            f"metadata_accuracy[{org_id}]", r.status, r.detail
                        ))
                except Exception as exc:
                    results.append(CheckResult(
                        f"metadata_accuracy[{org_id}]", "ERROR", str(exc)
                    ))

        passed = sum(1 for r in results if r.status == "PASS")
        return JSONResponse({
            "checks": [{"name": r.name, "status": r.status, "detail": r.detail} for r in results],
            "passed": passed,
            "total": len(results),
            "any_fail": passed < len(results),
            "ran_at": datetime.now(timezone.utc).isoformat(),
        })

    @mcp.custom_route("/admin/api/projects", methods=["GET"])
    async def admin_projects(request: Request) -> JSONResponse:
        """Projects from all active org connections. Orgs not yet connected are skipped."""
        require_admin()
        from ..auth import _org_router
        if _org_router is None:
            return JSONResponse({"projects": [], "note": "OrgRouter not initialized"})

        projects: list[dict] = []
        pools = getattr(_org_router, "_pools", {})
        for org_id, org_db in pools.items():
            try:
                rows = await org_db.list_projects()
                for r in rows:
                    projects.append({**r, "org_id": org_id})
            except Exception as exc:
                projects.append({"org_id": org_id, "error": str(exc)})

        projects.sort(key=lambda p: p.get("created_at") or "", reverse=True)
        return JSONResponse({"projects": projects})
