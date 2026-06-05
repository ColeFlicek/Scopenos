from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from .config_store import get_key_info, read_file_config, write_file_config
from .template import HTML


def register_routes(mcp, get_services) -> None:

    @mcp.custom_route("/ui", methods=["GET"])
    async def dashboard(request: Request) -> HTMLResponse:
        return HTMLResponse(HTML)

    @mcp.custom_route("/", methods=["GET"])
    async def root_redirect(request: Request) -> HTMLResponse:
        return HTMLResponse('<meta http-equiv="refresh" content="0;url=/ui">', status_code=302)

    @mcp.custom_route("/api/status", methods=["GET"])
    async def api_status(request: Request) -> JSONResponse:
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
            db = svcs["db"]
            embeddings = svcs["embeddings"]

            # ── Call graph ─────────────────────────────────────────────────
            async with db._db.execute("SELECT COUNT(*) FROM nodes") as cur:
                nodes = (await cur.fetchone())[0]
            async with db._db.execute("SELECT COUNT(*) FROM edges") as cur:
                edges = (await cur.fetchone())[0]
            result["layers"]["call_graph"] = {"status": "ok", "nodes": nodes, "edges": edges}

            # ── Embeddings (sqlite-vec) ─────────────────────────────────────
            async with db._db.execute("SELECT COUNT(*) FROM function_embeddings") as cur:
                emb_count = (await cur.fetchone())[0]
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

            # ── Projects ───────────────────────────────────────────────────
            # Isolated in its own try/except so a projects-query failure doesn't
            # clobber the already-collected layer health data above.
            try:
                async with db._db.execute("SELECT file, COUNT(*) FROM nodes GROUP BY file") as cur:
                    files = [(r[0], r[1]) for r in await cur.fetchall()]

                groups: dict[str, dict] = defaultdict(lambda: {"nodes": 0, "edges": 0, "embedded": 0})
                for f, count in files:
                    groups[_project_root(f)]["nodes"] += count

                for root in list(groups.keys()):
                    escaped = root.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    prefix = escaped + "/%"
                    async with db._db.execute(
                        "SELECT COUNT(*) FROM edges WHERE file LIKE ? ESCAPE '\\'", (prefix,)
                    ) as cur:
                        groups[root]["edges"] = (await cur.fetchone())[0]
                    async with db._db.execute(
                        """SELECT COUNT(*) FROM function_embeddings
                           WHERE id IN (SELECT id FROM nodes WHERE file LIKE ? ESCAPE '\\')""",
                        (prefix,)
                    ) as cur:
                        groups[root]["embedded"] = (await cur.fetchone())[0]

                result["projects"] = [{"path": k, **v} for k, v in sorted(groups.items())]
            except Exception as proj_exc:
                result["projects"] = [{"error": str(proj_exc)}]

        except Exception as exc:
            for layer in ("call_graph", "embeddings", "decisions"):
                if layer not in result["layers"]:
                    result["layers"][layer] = {"status": "error", "error": str(exc)}

        return JSONResponse(result)

    @mcp.custom_route("/api/config", methods=["POST"])
    async def api_save_config(request: Request) -> JSONResponse:
        try:
            data = await request.json()
            allowed = {"EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIM", "OLLAMA_BASE_URL"}
            write_file_config({k: v for k, v in data.items() if k in allowed})
            return JSONResponse({"status": "ok"})
        except Exception as exc:
            return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)

    @mcp.custom_route("/api/health", methods=["GET"])
    async def api_health(request: Request) -> JSONResponse:
        result: dict = {}
        try:
            svcs = await get_services()
            db = svcs["db"]
            embeddings = svcs["embeddings"]

            try:
                async with db._db.execute("SELECT COUNT(*) FROM nodes") as cur:
                    n = (await cur.fetchone())[0]
                result["call_graph"] = {"status": "ok", "node_count": n}
            except Exception as e:
                result["call_graph"] = {"status": "error", "error": str(e)}

            try:
                async with db._db.execute("SELECT COUNT(*) FROM function_embeddings") as cur:
                    n = (await cur.fetchone())[0]
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


def _project_root(file_path: str) -> str:
    parts = Path(file_path).parts
    depth = min(4, max(2, len(parts) - 1))
    return str(Path(*parts[:depth]))
