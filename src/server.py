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
from .client_setup import generate_setup_script, _default_claude_home
from .contracts.manager import ContractManager
from .decision_memory.memory import DecisionMemory
from .embeddings.embedder import EmbeddingStore
from .indexer import Indexer, _derive_project_id
from .web.routes import register_routes

# ── Service container ──────────────────────────────────────────────────────────

_services: dict[str, Any] = {}
_services_lock = asyncio.Lock()


async def _get_services() -> dict[str, Any]:
    """Return the shared service container, initializing it on first call."""
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
        contracts = ContractManager(db, embeddings)
        _services.update(
            db=db, embeddings=embeddings, decisions=decisions,
            indexer=indexer, contracts=contracts,
        )
    return _services


@asynccontextmanager
async def lifespan(server: FastMCP):
    """FastMCP lifespan — initialize all services on startup and close DB on shutdown."""
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
async def reembed_project(project_id: str) -> str:
    """
    Re-embed all functions for a project using the current two-tier embedding strategy,
    without touching the call graph or edges.

    Use this when:
    - A project was indexed before the two-tier embedding system (documented functions
      use the configured model; undocumented use text-embedding-3-large fallback)
    - Embeddings were corrupted or deleted but the call graph is intact
    - You want to switch embedding providers and re-embed everything

    This is a migration/recovery tool — normal re-indexing via index_project handles
    new and changed functions automatically. After this completes, call
    enrich_summaries(project_id) to upgrade undocumented functions to LLM-quality embeddings.
    """
    svcs = await _get_services()
    result = await svcs["indexer"].reembed_project(project_id)
    return json.dumps(result)


@mcp.tool()
async def enrich_summaries(project_id: str, limit: int = 500) -> str:
    """
    Generate LLM summaries for functions that were embedded using the large-model fallback
    (i.e., functions with no docstring or leading comment), then re-embed them with the
    configured model for better semantic search quality.

    This is intentionally user-initiated — it costs Anthropic API tokens (~$0.30 per 1,000
    functions) and may take several minutes for large batches.

    project_id: the project to enrich (must match the value used in index_project).
    limit: max functions to process in this call. Call repeatedly to enrich all functions.
    """
    svcs = await _get_services()
    result = await svcs["embeddings"].enrich_summaries(project_id, limit=limit)
    return json.dumps(result)


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


# ── Client setup tool ────────────────────────────────────────────────────────

@mcp.tool()
async def setup_acip_client(
    project_root: str,
    project_id: str = "",
    claude_home: str = "",
    install_git_hook: bool = True,
) -> str:
    """
    First-time setup for a new ACIP-integrated project. Call this once after
    connecting to the MCP server. Execute the returned script with Bash to
    complete setup — no other manual steps required.

    Installs on the client machine:
    - Pre-edit hook (~/.claude/hooks/acip-suggest.py): fires before every Edit,
      warns when editing chokepoints or risk-surface functions, and nudges toward
      ACIP tools on grep/Read.
    - Claude Code settings.json: adds Bash/Read/Edit PreToolUse hook matchers.
    - Project CLAUDE.md: three-tier workflow, pre-edit gate, multi-agent context.
    - Memory files: behavioral guidance that persists across sessions.
    - Post-commit git hook: re-indexes changed files + logs decisions on commit.

    project_root: absolute path to the project directory on the client machine
    project_id: slug for this project (default: derived from project_root basename)
    claude_home: path to ~/.claude directory (default: ~/.claude)
    install_git_hook: whether to install the post-commit git hook (default: true)

    Usage:
        result = setup_acip_client("/Users/name/myproject")
        # Then run: exec(result["setup_script"])  or  Bash(result["run_command"])
    """
    if not project_root:
        return json.dumps({"status": "error", "detail": "project_root is required"})

    pid = project_id or _derive_project_id(project_root)
    home = claude_home or _default_claude_home()
    server_url = os.getenv("ACIP_URL", "http://localhost:3004")

    # Load post-commit hook content from the scripts directory.
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    post_commit_path = os.path.join(scripts_dir, "post-commit.sh")
    try:
        with open(post_commit_path) as f:
            post_commit = f.read().replace(
                'ACIP_URL="${ACIP_URL:-http://localhost:3004}"',
                f'ACIP_URL="${{ACIP_URL:-{server_url}}}"',
            )
    except FileNotFoundError:
        post_commit = f'#!/usr/bin/env bash\n# post-commit hook\nexport ACIP_URL="{server_url}"\n'
        install_git_hook = False

    script = generate_setup_script(
        project_root=project_root,
        acip_url=server_url,
        project_id=pid,
        claude_home=home,
        install_git_hook=install_git_hook,
        post_commit_content=post_commit,
    )

    return json.dumps({
        "status": "ready",
        "project_id": pid,
        "acip_url": server_url,
        "run_command": "python3 << 'ACIP_SETUP'\n" + script + "\nACIP_SETUP",
        "setup_script": script,
        "instructions": (
            "Execute run_command with Bash to complete setup. "
            "Then restart Claude Code to activate hooks. "
            f"Then call index_project('{project_root}') to index the codebase."
        ),
    })


# ── Project Home tool ────────────────────────────────────────────────────────

@mcp.tool()
async def get_project_home(project_id: str) -> str:
    """
    Architectural intelligence snapshot for a project. Call this FIRST at the
    start of any session before reading files or forming an implementation plan.

    Returns in a single call:
    - subsystems: module groups with function counts, anchor class, and what it does
    - connections: which subsystems call which (the wiring diagram)
    - chokepoints: functions everything depends on — touch carefully
    - entry_points: top of the call graph (nothing calls these)
    - risk_surface: high-churn AND high-impact functions — highest change risk
    - health: contract compliance, top_knowledge_gaps (ranked by caller count), churn hotspots
    - recent_decisions: what changed in this codebase recently and why

    This replaces reading files for architectural understanding. After this call,
    use query_similar_functions / get_impact_radius for specific functions, then
    Read() only for exact implementation of the function you are about to modify.
    """
    svcs = await _get_services()
    result = await svcs["db"].get_project_home_data(project_id)
    return json.dumps(result)


# ── Contract tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def create_contract(
    title: str,
    natural_language: str,
    project_ids: list[str] | None = None,
) -> str:
    """
    Create a new Invariant Contract in draft mode.

    Parses the natural language rule using Claude, generates violation and
    compliance code examples, and stores a structural expression for the
    call-graph check. Returns the draft with generated examples — call
    approve_contract() to activate it, or update_contract_examples() to
    edit the examples first.

    title: short human-readable name for this contract
    natural_language: plain English rule (e.g. "all DB reads must go through read_secrets")
    project_ids: which projects this applies to. If omitted, requires explicit list.
    """
    svcs = await _get_services()
    result = await svcs["contracts"].generate_draft(
        project_ids=project_ids or [],
        title=title,
        natural_language=natural_language,
    )
    return json.dumps(result)


# update_contract_examples is intentionally NOT exposed as an MCP tool.
# Modifying violation/compliance examples on an active contract is a bypass
# vector — an agent could weaken examples so its own code no longer matches.
# This operation is available only through the web UI (human control plane).
# See: http_update_contract below.


@mcp.tool()
async def approve_contract(contract_id: str) -> str:
    """
    Activate a draft contract.

    Embeds all violation and compliance examples into vec0 tables so that
    semantic checking can run. Once active, the contract is enforced on
    every call to check_contracts() and via the post-commit hook.
    """
    svcs = await _get_services()
    result = await svcs["contracts"].approve(contract_id)
    return json.dumps(result)


@mcp.tool()
async def list_contracts(project_id: str = "") -> str:
    """
    List all contracts with their examples and status.

    project_id: filter to contracts that apply to this project. If omitted,
    returns all contracts across all projects.
    """
    svcs = await _get_services()
    result = await svcs["contracts"].list_contracts(project_id or None)
    return json.dumps(result)


@mcp.tool()
async def check_contracts(project_id: str) -> str:
    """
    Run all active contracts against the current call graph for a project.

    Returns a list of violations — both structural (call graph traversal)
    and semantic (embedding similarity against violation examples).
    """
    svcs = await _get_services()
    result = await svcs["contracts"].check_project(project_id)
    return json.dumps(result)


# delete_contract is intentionally NOT exposed as an MCP tool.
# An agent could delete a contract to bypass enforcement — this is the
# primary adversarial failure vector for the contracts system.
# Deletion is available only through the web UI (human control plane).
# See: http_delete_contract below.


# ── Agent Improvement Tools ───────────────────────────────────────────────────

@mcp.tool()
async def file_improvement(
    title: str,
    description: str,
    severity: str = "medium",
    project_id: str = "",
    affected_functions: list[str] | None = None,
    suggested_fix: str = "",
    reproduction_steps: str = "",
) -> str:
    """
    File a structured improvement report for ACIP so another agent session can
    implement it. Use this when you observe a bug, limitation, or enhancement
    opportunity during a session that you cannot or should not fix yourself.

    Write reports as if briefing a competent engineer who will pick this up cold:
    - title: one line, imperative ("Fix X", "Add Y", "Improve Z performance")
    - description: what is wrong or missing, why it matters, any relevant context
      you have (call paths observed, data shapes, error messages). Be specific —
      vague descriptions slow implementation.
    - severity: "low" | "medium" | "high" | "critical"
    - project_id: the ACIP project_id this applies to (empty = ACIP itself)
    - affected_functions: list of fully-qualified function IDs from the call graph
      (e.g. ["src.contracts.manager.ContractManager.check_project"]). Use
      query_similar_functions() first to find exact IDs.
    - suggested_fix: your best hypothesis for HOW to fix it. Even a rough sketch
      helps. Include rejected alternatives if you considered them.
    - reproduction_steps: exact steps or query that triggers the issue.

    Returns the improvement ID — save it if you need to reference this report later.
    """
    import uuid
    svcs = await _get_services()
    improvement_id = str(uuid.uuid4())
    result = await svcs["db"].create_improvement(
        improvement_id=improvement_id,
        project_id=project_id,
        title=title,
        description=description,
        affected_functions=affected_functions or [],
        severity=severity,
        suggested_fix=suggested_fix,
        reproduction_steps=reproduction_steps,
    )
    return json.dumps(result)


@mcp.tool()
async def list_improvements(
    project_id: str = "",
    status: str = "open",
) -> str:
    """
    List agent-filed improvement reports.

    Call this at session start on any ACIP project to see what prior agent
    sessions flagged as needing work. Improvements are ordered newest-first.

    project_id: filter to a specific project (empty = all projects)
    status: "open" | "done" | "wont_fix" | "" (empty = all statuses)

    Each result includes: id, title, description, severity, affected_functions,
    suggested_fix, reproduction_steps, filed_at.
    """
    svcs = await _get_services()
    result = await svcs["db"].list_improvements(
        project_id=project_id or None,
        status=status or None,
    )
    return json.dumps(result)


@mcp.tool()
async def resolve_improvement(
    improvement_id: str,
    resolution_notes: str,
    status: str = "done",
) -> str:
    """
    Mark an agent improvement report as resolved.

    Call this after implementing (or deciding not to implement) a filed improvement.
    Write resolution_notes that tell the next agent what was done:
    - Which files were modified
    - What the root cause turned out to be
    - Any trade-offs made during implementation
    - Why 'wont_fix' if that is the chosen status

    improvement_id: the ID returned by file_improvement()
    resolution_notes: what was done and why
    status: "done" | "wont_fix" (default: "done")
    """
    svcs = await _get_services()
    result = await svcs["db"].resolve_improvement(
        improvement_id=improvement_id,
        resolution_notes=resolution_notes,
        status=status,
    )
    return json.dumps(result)


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


# ── Browser search endpoint (used by the web UI search panel) ─────────────────

@mcp.custom_route("/api/search", methods=["POST"])
async def http_search(request: Request) -> JSONResponse:
    """
    POST /api/search {"snippet": "...", "project_id": "myapp", "top_k": 10}
    Runs query_similar_functions and returns results with similarity scores.
    Used by the web dashboard search panel.
    """
    try:
        data = await request.json()
        snippet    = data.get("snippet", "")
        project_id = data.get("project_id") or None
        top_k      = int(data.get("top_k", 10))
        if not snippet:
            return JSONResponse({"results": []})
        svcs = await _get_services()
        results = await svcs["embeddings"].query_similar(snippet, top_k, project_id)
        return JSONResponse({"results": results})
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


# ── Contracts HTTP endpoints ──────────────────────────────────────────────────

@mcp.custom_route("/api/contracts", methods=["GET"])
async def http_list_contracts(request: Request) -> JSONResponse:
    """GET /api/contracts?project_id=myapp"""
    try:
        project_id = request.query_params.get("project_id") or None
        svcs = await _get_services()
        result = await svcs["contracts"].list_contracts(project_id)
        return JSONResponse({"contracts": result})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts", methods=["POST"])
async def http_create_contract(request: Request) -> JSONResponse:
    """POST /api/contracts {title, natural_language, project_ids}"""
    try:
        data = await request.json()
        svcs = await _get_services()
        result = await svcs["contracts"].generate_draft(
            project_ids=data.get("project_ids", []),
            title=data.get("title", ""),
            natural_language=data.get("natural_language", ""),
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}", methods=["PUT"])
async def http_update_contract(request: Request) -> JSONResponse:
    """PUT /api/contracts/{id} {violation_examples, compliance_examples}"""
    try:
        contract_id = request.path_params["contract_id"]
        data = await request.json()
        svcs = await _get_services()
        result = await svcs["contracts"].update_examples(
            contract_id,
            data.get("violation_examples", []),
            data.get("compliance_examples", []),
        )
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}/approve", methods=["POST"])
async def http_approve_contract(request: Request) -> JSONResponse:
    """POST /api/contracts/{id}/approve"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        result = await svcs["contracts"].approve(contract_id)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}", methods=["DELETE"])
async def http_delete_contract(request: Request) -> JSONResponse:
    """DELETE /api/contracts/{id}"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        await svcs["contracts"].delete(contract_id)
        return JSONResponse({"status": "deleted"})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/contracts/{contract_id}/deactivate", methods=["POST"])
async def http_deactivate_contract(request: Request) -> JSONResponse:
    """POST /api/contracts/{id}/deactivate — sets status back to draft"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        await svcs["contracts"].deactivate(contract_id)
        return JSONResponse({"status": "ok"})
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
            violations = await svcs["contracts"].check_functions(project_id, function_ids)
        else:
            violations = await svcs["contracts"].check_project(project_id)
        return JSONResponse({"violations": violations})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/project-home/{project_id}", methods=["GET"])
async def http_project_home(request: Request) -> JSONResponse:
    """GET /api/project-home/{project_id} — architectural snapshot for web UI"""
    try:
        project_id = request.path_params["project_id"]
        svcs = await _get_services()
        result = await svcs["db"].get_project_home_data(project_id)
        return JSONResponse(result)
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


@mcp.custom_route("/api/violations", methods=["GET"])
async def http_list_violations(request: Request) -> JSONResponse:
    """GET /api/violations?project_id=myapp"""
    try:
        project_id = request.query_params.get("project_id") or None
        svcs = await _get_services()
        violations = await svcs["db"].list_violations(project_id)
        return JSONResponse({"violations": violations})
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=3004)
