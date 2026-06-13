from __future__ import annotations

import os

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .config_store import get_key_info, read_file_config, write_file_config
from .template import HTML


def register_routes(mcp, get_services) -> None:
    """Register all HTTP API and UI routes on the FastMCP server instance."""

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
            async with db._db.execute("SELECT COUNT(*) FROM nodes") as cur:
                nodes = (await cur.fetchone())[0]
            async with db._db.execute("SELECT COUNT(*) FROM edges") as cur:
                edges = (await cur.fetchone())[0]
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
            async with db._db.execute("SELECT COUNT(*) FROM decisions") as cur:
                dec_count = (await cur.fetchone())[0]
            async with db._db.execute(
                "SELECT COUNT(DISTINCT function_id) FROM decision_functions"
            ) as cur:
                linked = (await cur.fetchone())[0]
            async with db._db.execute("SELECT COUNT(*) FROM decision_embeddings") as cur:
                dec_emb_count = (await cur.fetchone())[0]
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
                for p in projects:
                    p["embedded"] = await embeddings.count_embeddings(p["id"])
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

    @mcp.custom_route("/api/health", methods=["GET"])
    async def api_health(request: Request) -> JSONResponse:
        """Return a lightweight health check result for all three ACIP layers."""
        result: dict = {}
        try:
            svcs = await get_services()
            db = svcs.db
            embeddings = svcs.embeddings

            try:
                async with db._db.execute("SELECT COUNT(*) FROM nodes") as cur:
                    n = (await cur.fetchone())[0]
                result["call_graph"] = {"status": "ok", "node_count": n}
            except Exception as e:
                result["call_graph"] = {"status": "error", "error": str(e)}

            try:
                n = await embeddings.count_embeddings()
                async with db._db.execute("SELECT COUNT(*) FROM decision_embeddings") as cur:
                    d = (await cur.fetchone())[0]
                result["embeddings"] = {
                    "status": "ok",
                    "function_vectors": n,
                    "decision_vectors": d,
                }
            except Exception as e:
                result["embeddings"] = {"status": "error", "error": str(e)}

            try:
                async with db._db.execute("SELECT COUNT(*) FROM decisions") as cur:
                    d = (await cur.fetchone())[0]
                result["decision_memory"] = {"status": "ok", "decision_count": d}
            except Exception as e:
                result["decision_memory"] = {"status": "error", "error": str(e)}

            result["embedding_config"] = {
                "provider": embeddings._provider,
                "model": embeddings._model,
                "dimensions": embeddings._dim,
                "storage": "sqlite-vec",
            }
        except Exception as exc:
            result["server"] = {"status": "error", "error": str(exc)}

        return JSONResponse(result)
