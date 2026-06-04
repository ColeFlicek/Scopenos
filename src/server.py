"""
ACIP — AI Code Intelligence Platform
FastMCP server exposing call graph, semantic embeddings,
and decision memory tools to Claude Code.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .call_graph.storage import CallGraphDB
from .decision_memory.memory import DecisionMemory
from .embeddings.embedder import EmbeddingStore
from .indexer import Indexer
from .web.routes import register_routes

# ── Service container ──────────────────────────────────────────────────────────

_services: dict[str, Any] = {}


async def _get_services() -> dict[str, Any]:
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


# ── Indexing tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def index_project(path: str) -> str:
    """
    Full index of a project directory. Builds the call graph, embeds all
    functions, and stores everything in SQLite + neo4j. Run once on initial
    setup; use index_changes for in-session updates.
    """
    svcs = await _get_services()
    result = await svcs["indexer"].index_project(path)
    return json.dumps(result)


@mcp.tool()
async def index_changes(file_paths: list[str], file_contents: dict[str, str], project_root: str = "") -> str:
    """
    Incremental update for changed files. Pass the paths and current contents
    of modified files. Stale call graph edges and embeddings are dropped and
    replaced. Pass project_root (same value used in index_project) to ensure
    module IDs are consistent with the full index.
    """
    svcs = await _get_services()
    result = await svcs["indexer"].index_changes(file_paths, file_contents, project_root=project_root)
    return json.dumps(result)


# ── Call graph tools ──────────────────────────────────────────────────────────

@mcp.tool()
async def get_callers(function_name: str) -> str:
    """
    Return all functions that call the specified function. Accepts a bare name,
    a qualified name (module.func), or a full id (module.Class.method).
    """
    svcs = await _get_services()
    results = await svcs["db"].get_callers(function_name)
    return json.dumps(results)


@mcp.tool()
async def get_callees(function_name: str) -> str:
    """
    Return all functions called by the specified function.
    """
    svcs = await _get_services()
    results = await svcs["db"].get_callees(function_name)
    return json.dumps(results)


@mcp.tool()
async def get_impact_radius(function_name: str, depth: int = 2) -> str:
    """
    BFS traversal outward from function_name up to `depth` levels.
    Returns the set of functions that would be impacted by a change to
    function_name, annotated with their distance from the origin.
    """
    svcs = await _get_services()
    results = await svcs["db"].get_impact_radius(function_name, depth)
    return json.dumps(results)


# ── Semantic embedding tool ───────────────────────────────────────────────────

@mcp.tool()
async def query_similar_functions(snippet: str, top_k: int = 10) -> str:
    """
    Return the top-k functions semantically similar to the provided snippet.
    Surfaces parallel implementations, related modules, and similar patterns
    that wouldn't appear in a grep or call trace.
    """
    svcs = await _get_services()
    results = await svcs["embeddings"].query_similar(snippet, top_k)
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
) -> str:
    """
    Write a decision entry to the decision memory.

    type: one of Architectural | Design | Implementation | Patch
    description: what was decided and why
    rejected_alternatives: what was tried or considered and not chosen
    trigger: cause of the decision (ticket ID, CVE, UX finding, etc.)
    linked_function_ids: list of function IDs this decision governs
    parent_decision_id: enables tree traversal from architectural down to patch level
    """
    svcs = await _get_services()
    result = await svcs["decisions"].log_decision(
        type=type,
        description=description,
        rejected_alternatives=rejected_alternatives,
        trigger=trigger,
        linked_function_ids=linked_function_ids,
        parent_decision_id=parent_decision_id,
    )
    return json.dumps(result)


@mcp.tool()
async def get_decision_history(function_name: str) -> str:
    """
    Return the full decision lineage for a function — every Architectural,
    Design, Implementation, and Patch decision linked to it, in chronological
    order. Call this before touching any function you didn't write.
    """
    svcs = await _get_services()
    results = await svcs["decisions"].get_decision_history(function_name)
    return json.dumps(results)


@mcp.tool()
async def query_decisions(query_text: str) -> str:
    """
    Semantic search over the full decision corpus. Useful for finding prior
    decisions that are relevant to a new change, even if they aren't linked
    to the specific function you're editing.
    """
    svcs = await _get_services()
    results = await svcs["decisions"].query_decisions(query_text)
    return json.dumps(results)


# ── Git hook HTTP endpoint ─────────────────────────────────────────────────────

@mcp.custom_route("/index", methods=["POST"])
async def git_hook_index(request: Request) -> JSONResponse:
    """
    POST /index {"changed_files": [...], "project_root": "/abs/path/to/repo"}
    Called by the post-commit git hook. project_root must match the value used
    in index_project so module IDs are consistent.
    """
    try:
        data = await request.json()
        changed_files: list[str] = data.get("changed_files", [])
        project_root: str = data.get("project_root", "")
        if not changed_files:
            return JSONResponse({"status": "no files"})

        file_contents: dict[str, str] = {}
        for fp in changed_files:
            try:
                from pathlib import Path as _Path
                content = _Path(fp).read_text(encoding="utf-8", errors="replace")
                file_contents[fp] = content
            except FileNotFoundError:
                pass  # deleted file — will be purged by index_changes

        svcs = await _get_services()
        result = await svcs["indexer"].index_changes(changed_files, file_contents, project_root=project_root)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=3004)
