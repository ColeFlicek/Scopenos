"""
Scopenos — Scopenos
FastMCP server exposing call graph, semantic embeddings,
and decision memory tools to Claude Code.
"""
from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from dataclasses import dataclass

from fastmcp import FastMCP
from starlette.exceptions import HTTPException  # noqa: F401
from starlette.requests import Request
from starlette.responses import JSONResponse

from .architecture_service import ArchitectureService
from .call_graph.storage import CallGraphDB
from .dependency_fingerprint import DependencyChecker
from .contracts.manager import ContractManager
from .decision_memory.memory import DecisionMemory
from .embeddings.embedder import EmbeddingStore
from .embeddings.pipeline import EmbeddingPipeline
from .indexer import Indexer, _derive_project_id
from .web.routes import register_routes
from .auth import AuthMiddleware, set_auth_db, get_current_user, check_permission, require_user
from .email_sender import get_email_sender
from .jobs import run_enrich_summaries
from .tools._shared import check_and_enqueue as _check_and_enqueue

# ── Service container ──────────────────────────────────────────────────────────

@dataclass
class Services:
    db: CallGraphDB
    embeddings: EmbeddingStore
    pipeline: EmbeddingPipeline
    decisions: DecisionMemory
    indexer: Indexer
    contracts: ContractManager
    checker: DependencyChecker
    arch: ArchitectureService | None = None


_services: Services | None = None
_services_lock = asyncio.Lock()


async def _get_services() -> Services:
    """Return the shared service container, initializing it on first call."""
    global _services
    if _services is not None:
        return _services
    async with _services_lock:
        if _services is not None:
            return _services
        db = await CallGraphDB.create()
        embeddings = await EmbeddingStore.create(db)
        pipeline = EmbeddingPipeline(db, embeddings)
        decisions = await DecisionMemory.create(db, embeddings)
        indexer = Indexer(db, pipeline)
        contracts = ContractManager(db, embeddings)
        _services = Services(
            db=db, embeddings=embeddings, pipeline=pipeline,
            decisions=decisions, indexer=indexer, contracts=contracts,
            checker=DependencyChecker(),
            arch=ArchitectureService(db),
        )
    return _services


@asynccontextmanager
async def lifespan(server: FastMCP):
    """FastMCP lifespan — initialize all services on startup and close DB on shutdown."""
    from .file_watcher import start_file_watcher
    svcs = await _get_services()
    set_auth_db(svcs.db)
    watcher_task = await start_file_watcher(svcs.db, svcs.indexer)
    yield
    watcher_task.cancel()
    try:
        await watcher_task
    except asyncio.CancelledError:
        pass
    if _services is not None:
        await _services.db.close()


# ── FastMCP server ────────────────────────────────────────────────────────────

_SCOPENOS_MCP_INSTRUCTIONS = """
Scopenos provides call graph traversal, semantic + keyword search, and decision memory for codebases.

Three-tier retrieval ladder — follow this order every session:

1. get_project_home(project_id) — one call, full architectural picture. Run first.
   Returns subsystems, chokepoints, entry points, recent decisions, and risk surface.

2. query_similar_functions(concept, project_id=...) — find which function to touch.
   Uses hybrid BM25 + semantic search (RRF fusion). Before editing any function:
   - get_impact_radius(fn, depth=2) — what breaks if this changes?
   - get_decision_history(fn) — why was it designed this way?

3. Read the file — only after you know exactly which lines to modify.

Core tools:
- get_project_home: subsystems, chokepoints, entry points, recent decisions
- query_similar_functions: hybrid semantic + keyword search across indexed codebase
- get_callers / get_callees: call graph traversal
- get_impact_radius(fn, depth=2): full recursive dependency tree
- get_decision_history: architectural decisions, rejected alternatives, concurrent edits
- log_decision: record trade-offs and rejected alternatives (call mid-session, not just at end)
- index_project_files: index a project from the CLIENT machine (sends file contents to server)
- index_changes: incremental update after editing files within a session
- check_contracts: verify invariants; contract_violations in index responses are blocking

project_id is a stable slug (e.g. "scopenos", "django"). Use the same value consistently.
Use index_project_files (not index_project) when the project lives on a different machine than the server.
"""

mcp = FastMCP("scopenos", lifespan=lifespan, instructions=_SCOPENOS_MCP_INSTRUCTIONS)
register_routes(mcp, _get_services, email_sender=get_email_sender())


# ── Tool registrations ─────────────────────────────────────────────────────────

from .tools import discovery, indexing, graph, memory, contracts, quality, dependencies

discovery.register(mcp, _get_services)
index_project, index_changes, enrich_summaries = indexing.register(mcp, _get_services)
graph.register(mcp, _get_services)
memory.register(mcp, _get_services)
contracts.register(mcp, _get_services)
quality.register(mcp, _get_services)
dependencies.register(mcp, _get_services)

# Re-exports for backward-compat with tests (symbols moved to src/tools/ during refactor)
from src import queue as _queue_mod  # noqa: E402
from .tools._shared import _USER_QUEUE_DEPTH_LIMIT  # noqa: E402


# ── Query HTTP endpoints ──────────────────────────────────────────────────────

@mcp.custom_route("/api/functions", methods=["POST"])
async def http_get_functions_for_files(request: Request) -> JSONResponse:
    """
    POST /api/functions {"files": [...], "project_id": "myapp"}
    Returns all function node IDs indexed for the given files and project.
    Used by the post-commit hook for function-level decision linkage.
    """
    try:
        svcs_pre = await _get_services()
        data = await request.json()
        _pid_ff = data.get("project_id", "")
        await _require_http_read(request, svcs_pre.db, _pid_ff)
        files: list[str] = data.get("files", [])
        project_id: str | None = data.get("project_id") or None
        if not files:
            return JSONResponse({"function_ids": []})
        db = svcs_pre.db
        ids: list[str] = []
        for fp in files:
            nodes = await db.get_nodes_by_file(fp, project_id)
            ids.extend(n["id"] for n in nodes)
        return JSONResponse({"function_ids": ids})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Browser search endpoint (used by the web UI search panel) ─────────────────

@mcp.custom_route("/api/search", methods=["POST"])
async def http_search(request: Request) -> JSONResponse:
    """
    POST /api/search {"snippet": "...", "project_id": "myapp", "top_k": 10}
    Runs query_similar_functions and returns results with similarity scores.
    Used by the web dashboard search panel.
    """
    try:
        svcs = await _get_services()
        data = await request.json()
        snippet    = data.get("snippet", "")
        project_id = data.get("project_id") or ""
        top_k      = int(data.get("top_k", 10))
        await _require_http_read(svcs.db, project_id)
        if not snippet:
            return JSONResponse({"results": []})
        results = await svcs.embeddings.query_similar(snippet, top_k, project_id or None)
        return JSONResponse({"results": results})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Projects HTTP endpoint ─────────────────────────────────────────────────────

@mcp.custom_route("/api/projects", methods=["GET"])
async def http_list_projects(request: Request) -> JSONResponse:
    """GET /api/projects — returns all registered projects with stats."""
    try:
        svcs = await _get_services()
        _user = require_user()
        _accessible = await svcs.db.get_accessible_project_ids(_user["id"])
        _all = await svcs.db.list_projects()
        projects = [p for p in _all if p["id"] in _accessible]
        return JSONResponse({"projects": projects})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── HTTP auth helpers ─────────────────────────────────────────────────────────

async def _require_http_read(db: "CallGraphDB", project_id: str = "") -> dict:
    """Validate auth and optionally check project read access."""
    user = require_user()
    if project_id:
        await check_permission(user, project_id, "read", db)
    return user


# ── Bulk index HTTP endpoint (used by scopenos-import slash command) ──────────────

@mcp.custom_route("/api/index-bulk", methods=["POST"])
async def http_index_bulk(request: Request) -> JSONResponse:
    """
    POST /api/index-bulk
    Body: {"project_root": "/abs/path", "project_id": "myapp",
           "files": {"abs/path/file.py": "<content>", ...}}
    Indexes a full project from file contents supplied by the caller — no server-side
    file I/O required. Used when the project lives on a different machine than the
    Scopenos server (e.g. the Claude Code workspace vs TheHive).
    project_id is derived from project_root's basename if not provided.
    """
    try:
        svcs = await _get_services()
        _user = require_user()
        data = await request.json()
        project_root: str = data.get("project_root", "")
        project_id: str = data.get("project_id", "") or _derive_project_id(project_root)
        # Auto-grant owner if this project has no owner yet (first index = you own it)
        if not await svcs.db.has_any_owner(project_id):
            await svcs.db.grant_project_access(_user["id"], project_id, "owner")
        await check_permission(_user, project_id, "write", svcs.db)
        files: dict[str, str] = data.get("files", {})
        if not files:
            return JSONResponse({"status": "error", "detail": "no files provided"}, status_code=400)
        result = await svcs.indexer.index_changes(
            list(files.keys()), files, project_root=project_root, project_id=project_id
        )
        written_ids = result.pop("function_ids", [])
        if written_ids:
            violations = await svcs.contracts.check_functions(project_id, written_ids)
            result["contract_violations"] = violations
        else:
            result["contract_violations"] = []
        return JSONResponse(result)
    except HTTPException:
        raise
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
        svcs = await _get_services()
        require_user()
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

        result = await svcs.indexer.index_changes(
            changed_files, file_contents, project_root=project_root, project_id=project_id
        )
        return JSONResponse(result)
    except HTTPException:
        raise
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
        require_user()
        result = await svcs.decisions.log_decision(
            type=data.get("type", "Patch"),
            description=description,
            rejected_alternatives=data.get("rejected_alternatives", ""),
            trigger=data.get("trigger", ""),
            linked_function_ids=data.get("linked_function_ids") or None,
            project_id=project_id,
        )
        return JSONResponse({"status": "ok", **result})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Contracts HTTP endpoints ──────────────────────────────────────────────────

@mcp.custom_route("/api/contracts", methods=["GET"])
async def http_list_contracts(request: Request) -> JSONResponse:
    """GET /api/contracts?project_id=myapp"""
    try:
        project_id = request.query_params.get("project_id") or ""
        svcs = await _get_services()
        await _require_http_read(svcs.db, project_id)
        result = await svcs.contracts.list_contracts(project_id or None)
        return JSONResponse({"contracts": result})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts", methods=["POST"])
async def http_create_contract(request: Request) -> JSONResponse:
    """POST /api/contracts {title, natural_language, project_ids, function_ids}"""
    try:
        svcs = await _get_services()
        _user = require_user()
        data = await request.json()
        for _pid in data.get("project_ids") or []:
            await check_permission(_user, _pid, "write", svcs.db)
        result = await svcs.contracts.generate_draft(
            project_ids=data.get("project_ids", []),
            title=data.get("title", ""),
            natural_language=data.get("natural_language", ""),
            function_ids=data.get("function_ids") or None,
        )
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}", methods=["PUT"])
async def http_update_contract(request: Request) -> JSONResponse:
    """PUT /api/contracts/{id} {violation_examples, compliance_examples}"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        _user = require_user()
        _ct = await svcs.db.get_contract(contract_id)
        for _pid in (_ct or {}).get("project_ids") or []:
            await check_permission(_user, _pid, "write", svcs.db)
        data = await request.json()
        result = await svcs.contracts.update_examples(
            contract_id,
            data.get("violation_examples", []),
            data.get("compliance_examples", []),
        )
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}/structural", methods=["PUT"])
async def http_update_contract_structural(request: Request) -> JSONResponse:
    """PUT /api/contracts/{id}/structural {"structural_expression": {...}}
    Replace the structural_expression on a contract in-place.
    Useful for correcting abstract LLM-generated patterns with concrete function names.
    """
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        _user = require_user()
        _ct = await svcs.db.get_contract(contract_id)
        for _pid in (_ct or {}).get("project_ids") or []:
            await check_permission(_user, _pid, "write", svcs.db)
        data = await request.json()
        expr = data.get("structural_expression")
        if not isinstance(expr, dict):
            return JSONResponse({"status": "error", "detail": "structural_expression must be a JSON object"}, status_code=400)
        result = await svcs.contracts.update_structural_expression(contract_id, expr)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}/approve", methods=["POST"])
async def http_approve_contract(request: Request) -> JSONResponse:
    """POST /api/contracts/{id}/approve"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        _user = require_user()
        _ct = await svcs.db.get_contract(contract_id)
        for _pid in (_ct or {}).get("project_ids") or []:
            await check_permission(_user, _pid, "write", svcs.db)
        result = await svcs.contracts.approve(contract_id)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# DELETE /api/projects/{id} and DELETE /api/contracts/{id} are intentionally
# not exposed as HTTP endpoints. Removing a project or contract is irreversible
# and was exploitable without auth. Projects cannot be deleted via API; contracts
# can be deactivated (POST .../deactivate) which preserves the audit trail.


@mcp.custom_route("/api/contracts/{contract_id}/deactivate", methods=["POST"])
async def http_deactivate_contract(request: Request) -> JSONResponse:
    """POST /api/contracts/{id}/deactivate — sets status back to draft"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        _user = require_user()
        _ct = await svcs.db.get_contract(contract_id)
        for _pid in (_ct or {}).get("project_ids") or []:
            await check_permission(_user, _pid, "write", svcs.db)
        await svcs.contracts.deactivate(contract_id)
        return JSONResponse({"status": "ok"})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/check", methods=["POST"])
async def http_check_contracts(request: Request) -> JSONResponse:
    """
    POST /api/contracts/check {project_id, function_ids}
    Called by the post-commit hook to check newly committed functions.
    """
    try:
        data = await request.json()
        project_id: str = data.get("project_id", "")
        function_ids: list[str] = data.get("function_ids", [])
        if not project_id:
            return JSONResponse({"violations": []})
        svcs = await _get_services()
        if function_ids:
            violations = await svcs.contracts.check_functions(project_id, function_ids)
        else:
            violations = await svcs.contracts.check_project(project_id)
        return JSONResponse({"violations": violations})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/project-home/{project_id}", methods=["GET"])
async def http_project_home(request: Request) -> JSONResponse:
    """GET /api/project-home/{project_id} — architectural snapshot for web UI"""
    try:
        project_id = request.path_params["project_id"]
        svcs = await _get_services()
        await _require_http_read(svcs.db, project_id)
        result = await svcs.arch.get_project_home(project_id, max_age_seconds=1800)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/violations", methods=["GET"])
async def http_list_violations(request: Request) -> JSONResponse:
    """GET /api/violations?project_id=myapp"""
    try:
        project_id = request.query_params.get("project_id") or ""
        svcs = await _get_services()
        await _require_http_read(svcs.db, project_id)
        violations = await svcs.db.list_violations(project_id or None)
        return JSONResponse({"violations": violations})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/reembed/{project_id}", methods=["POST"])
async def http_reembed_project(request: Request) -> JSONResponse:
    """POST /api/reembed/{project_id} — force re-embed all functions for a project."""
    try:
        project_id = request.path_params["project_id"]
        svcs = await _get_services()
        _user = require_user()
        await check_permission(_user, project_id, "write", svcs.db)
        result = await svcs.indexer.reembed_project(project_id)
        return JSONResponse(result)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/enrich-summaries/{project_id}", methods=["POST"])
async def http_enrich_summaries(request: Request) -> JSONResponse:
    """
    POST /api/enrich-summaries/{project_id}
    Optional body: {"limit": 500, "force": false}

    Enqueues an LLM summary generation job for functions that used the large-model
    fallback (no docstring/comment). Re-embeds them afterward for better search quality.
    Returns immediately with a job_id; the worker processes it in the background.
    """
    try:
        project_id = request.path_params["project_id"]
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        limit: int = int(body.get("limit", 500))
        force: bool = bool(body.get("force", False))
        svcs = await _get_services()
        user = require_user()
        await check_permission(user, project_id, "write", svcs.db)
        user_id = user["id"]
        try:
            job = _check_and_enqueue(user_id, run_enrich_summaries, project_id, limit, force, job_timeout=7200)
        except RuntimeError:
            return JSONResponse({"status": "rate_limited"}, status_code=429)
        return JSONResponse({"job_id": job.id, "status": "queued", "project_id": project_id})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Auth management HTTP endpoints ───────────────────────────────────────────

@mcp.custom_route("/setup", methods=["POST"])
async def http_setup(request: Request) -> JSONResponse:
    """POST /setup — first-time bootstrap.
    Creates the first user + API key. Returns 409 once any user exists.
    Body: {"email": "you@example.com", "name": "key name (optional)"}
    """
    try:
        svcs = await _get_services()
        if await svcs.db.has_any_users():
            return JSONResponse(
                {"detail": "Server already configured. Use 'scopenos auth rotate' to manage keys."},
                status_code=409,
            )
        data = await request.json()
        email = (data.get("email") or "").strip()
        if not email:
            return JSONResponse({"detail": "email is required"}, status_code=400)
        name = (data.get("name") or "primary").strip()
        user = await svcs.db.create_user(email, plan="owner")
        key = await svcs.db.create_api_key(user["id"], name)
        return JSONResponse({"key": key, "user_id": user["id"], "email": email})
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/auth/keys", methods=["GET"])
async def http_list_keys(request: Request) -> JSONResponse:
    """GET /api/auth/keys — list active API keys for the authenticated user."""
    try:
        svcs = await _get_services()
        user = require_user()
        keys = await svcs.db.list_api_keys(user["id"])
        return JSONResponse({"keys": keys})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/auth/keys", methods=["POST"])
async def http_create_key(request: Request) -> JSONResponse:
    """POST /api/auth/keys — create a new API key, optionally revoking the current one.
    Body: {"name": "...", "revoke_current": true}
    """
    try:
        svcs = await _get_services()
        user = require_user()
        data = await request.json()
        name = (data.get("name") or "rotated").strip()
        revoke_current = data.get("revoke_current", True)
        new_key = await svcs.db.create_api_key(user["id"], name)
        revoked_id = None
        if revoke_current:
            old_raw = request.headers.get("X-API-Key", "")
            revoked_id = await svcs.db.revoke_key_by_raw(old_raw, user["id"])
        return JSONResponse({"key": new_key, "revoked_id": revoked_id})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/auth/keys/{key_id}", methods=["DELETE"])
async def http_revoke_key(request: Request) -> JSONResponse:
    """DELETE /api/auth/keys/{key_id} — revoke a specific key by ID."""
    try:
        key_id = request.path_params["key_id"]
        svcs = await _get_services()
        user = require_user()
        revoked = await svcs.db.revoke_api_key(key_id, user["id"])
        if not revoked:
            return JSONResponse({"detail": "Key not found or already revoked"}, status_code=404)
        return JSONResponse({"status": "revoked", "id": key_id})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"detail": str(exc)}, status_code=500)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from starlette.middleware import Middleware
    mcp.run(
        transport="streamable-http", host="0.0.0.0", port=3004, stateless_http=False,
        middleware=[Middleware(AuthMiddleware)],
    )
