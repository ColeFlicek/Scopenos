"""
ACIP — AI Code Intelligence Platform
FastMCP server exposing call graph, semantic embeddings,
and decision memory tools to Claude Code.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .call_graph.storage import CallGraphDB
from .decision_memory.memory import DecisionMemory
from .embeddings.embedder import EmbeddingStore
from .indexer import Indexer, _derive_project_id
from .web.routes import register_routes

# ── Service container ──────────────────────────────────────────────────────────

_services: dict[str, Any] = {}
_services_lock = asyncio.Lock()


async def _get_services() -> dict[str, Any]:
    if _services:
        return _services
    async with _services_lock:
        if _services:
            return _services
        sqlite_path = os.getenv("SQLITE_PATH", "/data/acip.db")
        db = await CallGraphDB.create(sqlite_path)
        embeddings = await EmbeddingStore.create(db)
        decisions = await DecisionMemory.create(db, embeddings)
        indexer = Indexer(db, embeddings)
        _services.update(db=db, embeddings=embeddings, decisions=decisions, indexer=indexer)
    return _services


@asynccontextmanager
async def lifespan(server: FastMCP):
    await _get_services()
    yield
    if _services.get("db"):
        await _services["db"].close()


# ── FastMCP server ────────────────────────────────────────────────────────────

mcp = FastMCP("acip", lifespan=lifespan)
register_routes(mcp, _get_services)


# ── Project tools ─────────────────────────────────────────────────────────────

@mcp.tool()
async def list_projects() -> str:
    """
    List all indexed projects with their stats (node count, edge count,
    last indexed timestamp). Use this to discover available project_id values
    before calling scoped query tools.
    """
    svcs = await _get_services()
    result = await svcs["db"].list_projects()
    return json.dumps(result)


# ── Indexing tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def index_project(path: str, project_id: str = "") -> str:
    """
    Full index of a project directory. Builds the call graph, embeds all
    functions, and stores everything in SQLite + sqlite-vec. Run once on
    initial setup; use index_changes for in-session updates.

    project_id: slug to identify this project (e.g. "myapp"). If omitted,
    derived from the last path component.
    """
    svcs = await _get_services()
    result = await svcs["indexer"].index_project(path, project_id=project_id)
    return json.dumps(result)


@mcp.tool()
async def index_changes(
    file_paths: list[str],
    file_contents: dict[str, str],
    project_root: str = "",
    project_id: str = "",
) -> str:
    """
    Incremental update for changed files. Pass the paths and current contents
    of modified files. Stale call graph edges and embeddings are dropped and
    replaced. Pass project_root (same value used in index_project) to ensure
    module IDs are consistent with the full index.

    project_id: must match the value used in index_project. If omitted,
    derived from project_root's last component.
    """
    svcs = await _get_services()
    result = await svcs["indexer"].index_changes(
        file_paths, file_contents, project_root=project_root, project_id=project_id
    )
    return json.dumps(result)


# ── Call graph tools ──────────────────────────────────────────────────────────

@mcp.tool()
async def get_callers(function_name: str, project_id: str = "") -> str:
    """
    Return all functions that call the specified function. Accepts a bare name,
    a qualified name (module.func), or a full id (module.Class.method).

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    results = await svcs["db"].get_callers(function_name, project_id or None)
    return json.dumps(results)


@mcp.tool()
async def get_callees(function_name: str, project_id: str = "") -> str:
    """
    Return all functions called by the specified function.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    results = await svcs["db"].get_callees(function_name, project_id or None)
    return json.dumps(results)


@mcp.tool()
async def get_impact_radius(
    function_name: str, depth: int = 2, project_id: str = ""
) -> str:
    """
    BFS traversal outward from function_name up to `depth` levels.
    Returns the set of functions that would be impacted by a change to
    function_name, annotated with their distance from the origin.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    results = await svcs["db"].get_impact_radius(function_name, depth, project_id or None)
    return json.dumps(results)


# ── Semantic embedding tool ───────────────────────────────────────────────────

@mcp.tool()
async def query_similar_functions(
    snippet: str, top_k: int = 10, project_id: str = ""
) -> str:
    """
    Return the top-k functions semantically similar to the provided snippet.
    Surfaces parallel implementations, related modules, and similar patterns
    that wouldn't appear in a grep or call trace.

    project_id: limit results to a specific project. If omitted, searches across all projects.
    """
    svcs = await _get_services()
    results = await svcs["embeddings"].query_similar(snippet, top_k, project_id or None)
    return json.dumps(results)


# ── Decision memory tools ─────────────────────────────────────────────────────

@mcp.tool()
async def log_decision(
    type: str,
    description: str,
    rejected_alternatives: str = "",
    trigger: str = "",
    linked_function_ids: list[str] | None = None,
    parent_decision_id: str | None = None,
    project_id: str = "default",
) -> str:
    """
    Write a decision entry to the decision memory.

    type: one of Architectural | Design | Implementation | Patch
    description: what was decided and why
    rejected_alternatives: what was tried or considered and not chosen
    trigger: cause of the decision (ticket ID, CVE, UX finding, etc.)
    linked_function_ids: list of function IDs this decision governs
    parent_decision_id: enables tree traversal from architectural down to patch level
    project_id: project this decision belongs to (default: "default")
    """
    svcs = await _get_services()
    result = await svcs["decisions"].log_decision(
        type=type,
        description=description,
        rejected_alternatives=rejected_alternatives,
        trigger=trigger,
        linked_function_ids=linked_function_ids,
        parent_decision_id=parent_decision_id,
        project_id=project_id,
    )
    return json.dumps(result)


@mcp.tool()
async def get_decision_history(function_name: str, project_id: str = "") -> str:
    """
    Return the full decision lineage for a function — every Architectural,
    Design, Implementation, and Patch decision linked to it, in chronological
    order. Call this before touching any function you didn't write.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    results = await svcs["decisions"].get_decision_history(
        function_name, project_id or None
    )
    return json.dumps(results)


@mcp.tool()
async def query_decisions(
    query_text: str, project_id: str = ""
) -> str:
    """
    Semantic search over the full decision corpus. Useful for finding prior
    decisions that are relevant to a new change, even if they aren't linked
    to the specific function you're editing.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    results = await svcs["decisions"].query_decisions(
        query_text, project_id=project_id or None
    )
    return json.dumps(results)


# ── Query HTTP endpoints ──────────────────────────────────────────────────────

@mcp.custom_route("/api/functions", methods=["POST"])
async def http_get_functions_for_files(request: Request) -> JSONResponse:
    """
    POST /api/functions {"files": [...], "project_id": "myapp"}
    Returns all function node IDs indexed for the given files and project.
    Used by the post-commit hook for function-level decision linkage.
    """
    try:
        data = await request.json()
        files: list[str] = data.get("files", [])
        project_id: str | None = data.get("project_id") or None
        if not files:
            return JSONResponse({"function_ids": []})
        svcs = await _get_services()
        db = svcs["db"]
        ids: list[str] = []
        for fp in files:
            nodes = await db.get_nodes_by_file(fp, project_id)
            ids.extend(n["id"] for n in nodes)
        return JSONResponse({"function_ids": ids})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Projects HTTP endpoint ─────────────────────────────────────────────────────

@mcp.custom_route("/api/projects", methods=["GET"])
async def http_list_projects(request: Request) -> JSONResponse:
    """GET /api/projects — returns all registered projects with stats."""
    try:
        svcs = await _get_services()
        projects = await svcs["db"].list_projects()
        return JSONResponse({"projects": projects})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Bulk index HTTP endpoint (used by acip-import slash command) ──────────────

@mcp.custom_route("/api/index-bulk", methods=["POST"])
async def http_index_bulk(request: Request) -> JSONResponse:
    """
    POST /api/index-bulk
    Body: {"project_root": "/abs/path", "project_id": "myapp",
           "files": {"abs/path/file.py": "<content>", ...}}
    Indexes a full project from file contents supplied by the caller — no server-side
    file I/O required. Used when the project lives on a different machine than the
    ACIP server (e.g. the Claude Code workspace vs TheHive).
    project_id is derived from project_root's basename if not provided.
    """
    try:
        data = await request.json()
        project_root: str = data.get("project_root", "")
        project_id: str = data.get("project_id", "") or _derive_project_id(project_root)
        files: dict[str, str] = data.get("files", {})
        if not files:
            return JSONResponse({"status": "error", "detail": "no files provided"}, status_code=400)
        svcs = await _get_services()
        result = await svcs["indexer"].index_changes(
            list(files.keys()), files, project_root=project_root, project_id=project_id
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Git hook HTTP endpoint ─────────────────────────────────────────────────────

@mcp.custom_route("/index", methods=["POST"])
async def git_hook_index(request: Request) -> JSONResponse:
    """
    POST /index {"changed_files": [...], "project_root": "/abs/path/to/repo",
                 "project_id": "myapp"}
    Called by the post-commit git hook. project_root must match the value used
    in index_project so module IDs are consistent.
    project_id is derived from project_root's basename if not provided.
    """
    try:
        data = await request.json()
        changed_files: list[str] = data.get("changed_files", [])
        project_root: str = data.get("project_root", "")
        project_id: str = data.get("project_id", "") or _derive_project_id(project_root)
        if not changed_files:
            return JSONResponse({"status": "no files"})

        file_contents: dict[str, str] = {}
        for fp in changed_files:
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                file_contents[fp] = content
            except FileNotFoundError:
                pass  # deleted file — will be purged by index_changes

        svcs = await _get_services()
        result = await svcs["indexer"].index_changes(
            changed_files, file_contents, project_root=project_root, project_id=project_id
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Decision HTTP endpoint ────────────────────────────────────────────────────

@mcp.custom_route("/api/decisions", methods=["POST"])
async def http_log_decision(request: Request) -> JSONResponse:
    """
    POST /api/decisions
    Body: {"type": "Patch|Implementation|Design|Architectural",
           "description": "...",
           "rejected_alternatives": "...",
           "trigger": "...",
           "linked_function_ids": [...],
           "project_id": "myapp"}
    Called by the post-commit git hook and backfill scripts.
    Mirrors the log_decision MCP tool without requiring MCP transport.
    """
    try:
        data = await request.json()
        description = data.get("description", "")
        if not description:
            return JSONResponse({"status": "error", "detail": "description required"}, status_code=400)
        project_id = data.get("project_id") or "default"
        svcs = await _get_services()
        result = await svcs["decisions"].log_decision(
            type=data.get("type", "Patch"),
            description=description,
            rejected_alternatives=data.get("rejected_alternatives", ""),
            trigger=data.get("trigger", ""),
            linked_function_ids=data.get("linked_function_ids") or None,
            project_id=project_id,
        )
        return JSONResponse({"status": "ok", **result})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=3004)
