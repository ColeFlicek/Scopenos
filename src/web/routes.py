from __future__ import annotations

import asyncio
import os
from typing import Awaitable, Callable

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .config_store import get_key_info, read_file_config, write_file_config
from .template import HTML


def register_routes(
    mcp,
    get_services,
    email_sender: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Register all HTTP API and UI routes on the FastMCP server instance."""

    @mcp.custom_route("/api/signup", methods=["POST"])
    async def api_signup(request: Request) -> JSONResponse:
        """Create a user, provision an org database, and email their API key.

        Body: {email: str, org_slug: str (optional)}

        Multi-org mode (PROVISIONER_DSN is set):
          - org_slug is required; must be unique, e.g. "acme" → org_acme DB.
          - A dedicated Postgres database is provisioned for the org.
          - The API key is scoped to that org; all indexed data goes there.

        Single-tenant mode (PROVISIONER_DSN not set):
          - org_slug is ignored.
          - User and key are created in the control / single DB.
          - Backward-compatible with the existing deployment.
        """
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "detail": "Invalid JSON"}, status_code=400)

        email = (data.get("email") or "").strip()
        if not email:
            return JSONResponse({"status": "error", "detail": "email is required"}, status_code=400)

        provisioner_dsn = os.getenv("PROVISIONER_DSN", "")
        org_slug = (data.get("org_slug") or "").strip()

        if provisioner_dsn:
            # ── Multi-org mode ────────────────────────────────────────────────
            if not org_slug:
                return JSONResponse(
                    {"status": "error", "detail": "org_slug is required in multi-org mode"},
                    status_code=400,
                )

            from ..provisioning import provision_org
            from ..call_graph.storage import CallGraphDB

            control_dsn = (
                os.getenv("CONTROL_DB_URL")
                or os.getenv("DATABASE_URL", "")
            )

            try:
                prov_result = await provision_org(
                    slug=org_slug,
                    provisioner_dsn=provisioner_dsn,
                    control_dsn=control_dsn,
                )
            except Exception as exc:
                msg = str(exc)
                if "already exists" in msg.lower():
                    return JSONResponse(
                        {"status": "error", "detail": f"org '{org_slug}' already exists"},
                        status_code=409,
                    )
                return JSONResponse({"status": "error", "detail": msg}, status_code=500)

            # Create user + key in the control DB, scoped to the new org
            control_db = await CallGraphDB.create(control_dsn)
            try:
                try:
                    user = await control_db.create_user(email)
                except Exception:
                    existing = await control_db.get_user_by_email(email)
                    if existing is None:
                        return JSONResponse(
                            {"status": "error", "detail": "could not create or find user"},
                            status_code=500,
                        )
                    user = existing

                raw_key = await control_db.create_api_key(user["id"], name="signup", org_id=org_slug)
            finally:
                await control_db.close()

            if email_sender is not None:
                await email_sender(email, raw_key)

            return JSONResponse({
                "status": "ok",
                "message": "Check your email for your API key",
                "org": org_slug,
                "db": prov_result["db_name"],
            })

        else:
            # ── Single-tenant mode ────────────────────────────────────────────
            svcs = await get_services()
            db = svcs.db

            try:
                user = await db.create_user(email)
            except Exception:
                existing = await db.get_user_by_email(email)
                if existing is None:
                    return JSONResponse(
                        {"status": "error", "detail": "could not create or find user"},
                        status_code=500,
                    )
                user = existing

            raw_key = await db.create_api_key(user["id"], name="signup")

            if email_sender is not None:
                await email_sender(email, raw_key)

            return JSONResponse({"status": "ok", "message": "Check your email for your API key"})

    @mcp.custom_route("/ui", methods=["GET"])
    async def dashboard(request: Request) -> HTMLResponse:
        """Serve the web dashboard HTML page."""
        return HTMLResponse(HTML)

    @mcp.custom_route("/", methods=["GET"])
    async def root_redirect(request: Request) -> HTMLResponse:
        """Redirect the root path to the web dashboard."""
        return HTMLResponse('<meta http-equiv="refresh" content="0;url=/ui">', status_code=302)

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
        """Return a full status snapshot covering all layers, projects, and config."""
        result: dict = {
            "layers": {},
            "config": {},
            "pending_config": read_file_config(),
            "keys": get_key_info(),
            "projects": [],
            "config_differs": False,
        }

        try:
            svcs = await get_services()
            db = svcs.db
            embeddings = svcs.embeddings

            # ── Call graph ─────────────────────────────────────────────────
            nodes = await db.count_nodes()
            edges = await db.count_edges()
            result["layers"]["call_graph"] = {"status": "ok", "nodes": nodes, "edges": edges}

            # ── Embeddings (sqlite-vec, one table per project) ──────────────
            emb_count = await embeddings.count_embeddings()
            result["layers"]["embeddings"] = {
                "status": "ok",
                "functions": emb_count,
                "provider": embeddings._provider,
                "model": embeddings._model,
                "dim": embeddings._dim,
            }

            # ── Decision memory ────────────────────────────────────────────
            dec_count = await db.count_decisions()
            linked = await db.count_decision_function_links()
            dec_emb_count = await db.count_decision_embeddings()
            result["layers"]["decisions"] = {
                "status": "ok",
                "count": dec_count,
                "linked_functions": linked,
                "embedded": dec_emb_count,
            }

            # ── Running config ─────────────────────────────────────────────
            running = {
                "EMBEDDING_PROVIDER": embeddings._provider,
                "EMBEDDING_MODEL": embeddings._model,
                "EMBEDDING_DIM": str(embeddings._dim),
                "OLLAMA_BASE_URL": os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            }
            result["config"] = running

            pending = result["pending_config"]
            result["config_differs"] = any(
                pending.get(k) and str(pending[k]) != str(running.get(k, ""))
                for k in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIM")
            )

            # ── Projects (from projects table + per-project embedding counts) ─
            try:
                projects = await db.list_projects()
                counts = await embeddings.count_embeddings_by_project()
                for p in projects:
                    p["embedded"] = counts.get(p["id"], 0)
                result["projects"] = projects
            except Exception as proj_exc:
                result["projects"] = [{"error": str(proj_exc)}]

        except Exception as exc:
            for layer in ("call_graph", "embeddings", "decisions"):
                if layer not in result["layers"]:
                    result["layers"][layer] = {"status": "error", "error": str(exc)}

        return JSONResponse(result)

    @mcp.custom_route("/api/config", methods=["POST"])
    async def api_save_config(request: Request) -> JSONResponse:
        """Persist allowed embedding configuration keys to config.json."""
        try:
            data = await request.json()
            allowed = {"EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIM", "OLLAMA_BASE_URL"}
            write_file_config({k: v for k, v in data.items() if k in allowed})
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

    @mcp.custom_route("/api/jobs/{job_id}", methods=["GET"])
    async def api_job_status(request: Request) -> JSONResponse:
        """Return the status of a background job by ID."""
        job_id = request.path_params.get("job_id", "")
        try:
            from rq.job import Job, NoSuchJobError
            from src.queue import get_redis
            job = Job.fetch(job_id, connection=get_redis())
            status = job.get_status()
            return JSONResponse({
                "job_id": job_id,
                "status": status.value if hasattr(status, "value") else str(status),
                "result": job.result if job.is_finished else None,
                "error": str(job.exc_info)[:500] if job.is_failed else None,
            })
        except Exception as exc:
            if "NoSuchJobError" in type(exc).__name__ or "No such job" in str(exc):
                return JSONResponse({"error": "job not found"}, status_code=404)
            return JSONResponse({"error": str(exc)}, status_code=500)

    @mcp.custom_route("/api/projects/{project_id}", methods=["PATCH"])
    async def api_rename_project(request: Request) -> JSONResponse:
        """PATCH /api/projects/{project_id} {"name": "new-name"} — rename a project's display name."""
        try:
            project_id = request.path_params["project_id"]
            data = await request.json()
            new_name = (data.get("name") or "").strip()
            if not new_name:
                return JSONResponse({"status": "error", "detail": "name is required"}, status_code=400)
            svcs = await get_services()
            found = await svcs.db.rename_project(project_id, new_name)
            if not found:
                return JSONResponse({"status": "error", "detail": "project not found"}, status_code=404)
            return JSONResponse({"status": "ok", "project_id": project_id, "name": new_name})
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

    @mcp.custom_route("/api/me", methods=["GET"])
    async def api_me(request: Request) -> JSONResponse:
        """GET /api/me — returns the authenticated user's profile and accessible projects."""
        from ..auth import require_user
        try:
            user = require_user()
            svcs = await get_services()
            projects = await svcs.db.list_user_projects(user["id"])
            return JSONResponse({"user": user, "projects": projects})
        except HTTPException:
            raise
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

    @mcp.custom_route("/api/health", methods=["GET"])
    async def api_health(request: Request) -> JSONResponse:
        """Liveness probe — returns 200 if the HTTP server process is up.

        Intentionally does not check the database. Use /api/ready for that.
        K8s liveness: a DB failure should remove the pod from rotation (readiness),
        not restart it (liveness). Restarting can't fix a missing DB grant.
        """
        return JSONResponse({"status": "ok"})

    @mcp.custom_route("/api/ready", methods=["GET"])
    async def api_ready(request: Request) -> JSONResponse:
        """Readiness probe — returns 200 only if the DB auth path is working.

        Runs SELECT 1 FROM users via the control DB connection (the same role
        the server uses for every auth check). A failure means the pod should
        stop receiving traffic: either the DB is unreachable or the role lacks
        the grants it needs to resolve API keys.
        """
        from ..auth import get_control_db
        db = get_control_db()
        if db is None:
            return JSONResponse(
                {"status": "not_ready", "detail": "control DB not initialized"},
                status_code=503,
            )
        try:
            await db.ping()
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse(
                {"status": "not_ready", "detail": str(exc)},
                status_code=503,
            )
