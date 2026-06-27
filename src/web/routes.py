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
        """Create a user and email their API key. Body: {email: str}."""
        try:
            data = await request.json()
        except Exception:
            return JSONResponse({"status": "error", "detail": "Invalid JSON"}, status_code=400)

        email = (data.get("email") or "").strip()
        if not email:
            return JSONResponse({"status": "error", "detail": "email is required"}, status_code=400)

        svcs = await get_services()
        db = svcs.db

        try:
            user = await db.create_user(email)
        except Exception:
            async with db._db.execute(
                "SELECT id, email, plan FROM users WHERE email = ?", (email,)
            ) as cur:
                row = await cur.fetchone()
            user = dict(row)

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
        """Return a lightweight health check result for all three Scopenos layers."""
        result: dict = {}

        async def _check() -> None:
            svcs = await get_services()
            db = svcs.db
            embeddings = svcs.embeddings

            try:
                n = await db.count_nodes()
                result["call_graph"] = {"status": "ok", "node_count": n}
            except Exception as e:
                result["call_graph"] = {"status": "error", "error": str(e)}

            try:
                n = await embeddings.count_embeddings()
                d = await db.count_decision_embeddings()
                result["embeddings"] = {
                    "status": "ok",
                    "function_vectors": n,
                    "decision_vectors": d,
                }
            except Exception as e:
                result["embeddings"] = {"status": "error", "error": str(e)}

            try:
                d = await db.count_decisions()
                result["decision_memory"] = {"status": "ok", "decision_count": d}
            except Exception as e:
                result["decision_memory"] = {"status": "error", "error": str(e)}

            result["embedding_config"] = {
                "provider": embeddings._provider,
                "model": embeddings._model,
                "dimensions": embeddings._dim,
                "storage": "pgvector",
            }

        try:
            await asyncio.wait_for(_check(), timeout=5.0)
        except asyncio.TimeoutError:
            result["server"] = {"status": "error", "error": "health check timed out (DB unreachable?)"}
        except Exception as exc:
            result["server"] = {"status": "error", "error": str(exc)}

        status_code = 200 if "server" not in result else 503
        return JSONResponse(result, status_code=status_code)
