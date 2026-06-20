"""
Phronosis — Phronosis
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
from .client_setup import generate_setup_script, _default_claude_home
from .dependency_fingerprint import (
    DependencyChecker,
    DependencyFingerprint,
    DependencyFingerprinter,
    fingerprint_from_row,
    fingerprint_payload,
)
from .contracts.manager import ContractManager
from .decision_memory.memory import DecisionMemory
from .embeddings.embedder import EmbeddingStore
from .embeddings.pipeline import EmbeddingPipeline
from .indexer import Indexer, _derive_project_id
from .web.routes import register_routes
from .auth import AuthMiddleware, set_auth_db, get_current_user, check_permission
from .email_sender import get_email_sender
from . import queue as _queue_mod
from .jobs import run_index_project, run_enrich_summaries, run_reembed_project

# Max jobs a user may have in QUEUED or STARTED state across all job types.
_USER_QUEUE_DEPTH_LIMIT = 3


def _check_and_enqueue(user_id: str, fn, *args, job_timeout: int = 3600):
    """Enforce per-user queue depth limit, then enqueue the job.

    Uses a Redis sorted set (score = expiry epoch) so per-member TTL is
    approximated without worker-side callbacks. Expired entries are pruned
    before counting so stale jobs don't block new ones indefinitely.

    Raises RuntimeError with status "rate_limited" if the user is at the limit.
    Returns the enqueued Job.
    """
    import time
    from rq.job import Job, JobStatus

    q = _queue_mod.get_queue()
    redis = q.connection
    depth_key = f"phronosis:user_queue_depth:{user_id}"
    now = time.time()

    # Remove entries whose TTL has expired (score < now).
    redis.zremrangebyscore(depth_key, "-inf", now)

    # Count remaining active jobs, verifying they're still queued/started.
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

    job = q.enqueue(fn, *args, job_timeout=job_timeout)
    expire_at = now + job_timeout
    redis.zadd(depth_key, {job.id: expire_at})
    redis.expireat(depth_key, int(expire_at) + 60)
    return job

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

mcp = FastMCP("phronosis", lifespan=lifespan)
mcp.add_middleware(AuthMiddleware())
register_routes(mcp, _get_services, email_sender=get_email_sender())


# ── Project tools ─────────────────────────────────────────────────────────────

@mcp.tool()
async def list_projects() -> str:
    """
    List all indexed projects with their stats (node count, edge count,
    last indexed timestamp). Use this to discover available project_id values
    before calling scoped query tools.
    """
    svcs = await _get_services()
    user = get_current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    accessible = await svcs.db.get_accessible_project_ids(user["id"])
    all_projects = await svcs.db.list_projects()
    result = [p for p in all_projects if p["id"] in accessible]
    return json.dumps(result)


# ── Branch / diff tools ───────────────────────────────────────────────────────

@mcp.tool()
async def compare_branches(project_id_a: str, project_id_b: str) -> str:
    """
    Diff two indexed project snapshots at the call-graph level.

    Returns functions added (in B, not A), removed (in A, not B), and changed
    (same function ID, different body). Designed for branch comparison — index
    each branch as a separate project_id (e.g. "myapp/main" vs "myapp/feature-x")
    then call this to see what the feature branch changed.

    project_id_a: the base (e.g. "myapp/main")
    project_id_b: the head (e.g. "myapp/feature-x")
    """
    svcs = await _get_services()
    await _check_read_access(project_id_a, svcs.db)
    await _check_read_access(project_id_b, svcs.db)
    result = await svcs.db.compare_projects(project_id_a, project_id_b)
    return json.dumps(result)


@mcp.tool()
async def get_branch_conflicts(
    project_id: str,
    function_ids: list[str],
    current_branch: str = "",
) -> str:
    """
    Conflict detection for shared project indexes.

    Given a list of functions you are currently working on, returns any other
    branches (or main) that have recently modified the same functions. Use this
    before starting a significant edit to see if a teammate or concurrent agent
    has already touched the same code.

    project_id: the shared project index (same one all branches index into)
    function_ids: full function IDs you plan to edit (e.g. ["src.auth.login"])
    current_branch: your current branch — excluded from results so you only see
                    competing changes. If omitted, all branches are returned.

    Returns:
      conflicts: list of {function_id, competing_branches: [{branch, head_commit,
                 modified_at}], main_drift: bool}
      main_drift: functions where main/master was recently modified — indicates
                  your branch may be behind and could conflict on merge
      summary: {total, branches, functions_with_main_drift}
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    result = await svcs.db.get_branch_conflicts(project_id, function_ids, current_branch)
    return json.dumps(result)


@mcp.tool()
async def get_function_at_commit(
    function_id: str, commit_hash: str, project_id: str = ""
) -> str:
    """
    Read a function's implementation at a specific git commit without modifying
    the stored index. Answers point-in-time questions: "what did this function
    look like 3 months ago?" or "what was it before this refactor?"

    function_id: full function ID (e.g. "src.auth.authenticate_user")
    commit_hash: full or short git commit hash
    project_id: project to resolve the function's file path. If omitted,
    searches all projects.

    Requires the git repo to be accessible at the project root on the server.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    pid = project_id or None

    # Resolve function → file path from the stored index.
    hits = await svcs.db.find_node_by_name(function_id, pid)
    if not hits:
        return json.dumps({"error": f"Function '{function_id}' not found in index."})

    node = hits[0]
    file_path = node.get("file", "")
    node_project = node.get("project_id", project_id)

    # Get project root to find the git repo.
    projects = await svcs.db.list_projects()
    root = next((p["root"] for p in projects if p["id"] == node_project), "")
    if not root:
        return json.dumps({"error": f"Project root not found for '{node_project}'."})

    # Make file path relative to project root for git show.
    try:
        rel_path = os.path.relpath(file_path, root)
    except ValueError:
        rel_path = file_path

    import subprocess as _sp
    try:
        result = _sp.run(
            ["git", "show", f"{commit_hash}:{rel_path}"],
            capture_output=True, text=True, cwd=root, timeout=10,
        )
        if result.returncode != 0:
            return json.dumps({
                "error": f"git show failed: {result.stderr.strip()}",
                "commit": commit_hash, "file": rel_path,
            })
        content = result.stdout
    except Exception as exc:
        return json.dumps({"error": str(exc)})

    # Parse just the target function from the historical content.
    from .call_graph.parser import TreeSitterParser
    parser = TreeSitterParser()
    nodes, _ = parser.parse_file(file_path, content, project_root=root)
    fn_name = function_id.split(".")[-1]
    match = next(
        (n for n in nodes if n.name == fn_name or n.id == function_id), None
    )

    if not match:
        return json.dumps({
            "note": f"Function '{fn_name}' not found at commit {commit_hash[:8]} — "
                    "it may not have existed yet or had a different name.",
            "commit": commit_hash[:8],
            "file_content_length": len(content),
        })

    return json.dumps({
        "function_id": function_id,
        "commit": commit_hash[:8],
        "name": match.name,
        "signature": match.signature,
        "body": match.body,
        "file": rel_path,
    })


# ── Indexing tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def estimate_index(path: str) -> str:
    """
    Quick pre-scan of a project directory: counts functions by regex and
    returns an estimated index time before committing to a full index.

    Use this before index_project to give the user a heads-up — present the
    result in natural language and ask for confirmation before proceeding.
    Runs in under 1 second on any project size (no DB, no embedding, no AST).

    Returns: {"files": N, "estimated_functions": N, "estimated_seconds": N}
    """
    from .indexer import estimate_project
    if not os.path.exists(path):
        return json.dumps({"error": f"Path not found: {path}"})
    return json.dumps(estimate_project(path))


@mcp.tool()
async def index_project(path: str, project_id: str = "", branch: str = "") -> str:
    """
    Full index of a project directory. Builds the call graph, embeds all
    functions, and stores everything in Postgres + pgvector. Run once on
    initial setup; use index_changes for in-session updates.

    project_id: slug to identify this project (e.g. "myapp"). If omitted,
    derived from the last path component.
    branch: git branch name to record. If omitted, auto-detected from the
    repo at path. Use this to index multiple branches of the same repo as
    separate project_ids (e.g. project_id="myapp/feature-x", branch="feature-x").
    """
    svcs = await _get_services()
    pid = project_id or Path(path).name or "default"
    await check_permission(get_current_user(), pid, "write", svcs.db)
    user = get_current_user()
    user_id = user["id"] if user else "anon"
    try:
        job = _check_and_enqueue(user_id, run_index_project, path, pid, job_timeout=3600)
    except RuntimeError:
        return json.dumps({"status": "rate_limited"})
    hook_path = Path(path) / ".git" / "hooks" / "post-commit"
    hook_installed = hook_path.exists() and os.access(hook_path, os.X_OK)
    response: dict = {"job_id": job.id, "status": "queued"}
    if not hook_installed:
        response["hook_missing"] = True
        response["hook_warning"] = (
            "No executable post-commit hook found at .git/hooks/post-commit. "
            "Contract violations from direct git commits will not be detected. "
            "Install with: cp /path/to/phronosis/scripts/post-commit.sh "
            ".git/hooks/post-commit && chmod +x .git/hooks/post-commit"
        )
    return json.dumps(response)


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

    Contract violations detected in the written functions are returned inline
    under "contract_violations". An empty list means no active contracts fired.
    Fix or acknowledge violations before proceeding — writing code that violates
    an active contract is the primary adversarial failure vector for the system.
    """
    svcs = await _get_services()
    pid = project_id or (_derive_project_id(project_root) if project_root else "default")
    await check_permission(get_current_user(), pid, "write", svcs.db)
    result = await svcs.indexer.index_changes(
        file_paths, file_contents, project_root=project_root, project_id=project_id
    )
    written_ids = result.pop("function_ids", [])
    if written_ids:
        violations = await svcs.contracts.check_functions(pid, written_ids)
        result["contract_violations"] = violations
    else:
        result["contract_violations"] = []
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
    await check_permission(get_current_user(), project_id, "write", svcs.db)
    user = get_current_user()
    user_id = user["id"] if user else "anon"
    try:
        job = _check_and_enqueue(user_id, run_reembed_project, project_id, job_timeout=3600)
    except RuntimeError:
        return json.dumps({"status": "rate_limited"})
    return json.dumps({"job_id": job.id, "status": "queued"})


@mcp.tool()
async def enrich_summaries(
    project_id: str, limit: int = 500, force: bool = False
) -> str:
    """
    Generate LLM summaries for functions that were embedded using the large-model fallback
    (i.e., functions with no docstring or leading comment), then re-embed them with the
    configured model for better semantic search quality.

    This is intentionally user-initiated — it costs Anthropic API tokens (~$0.30 per 1,000
    functions) and may take several minutes for large batches.

    Calling enrich_summaries repeatedly without force=True is safe and cheap — functions
    that already have a summary are skipped automatically.

    force: if True, re-summarize and re-embed even functions that already have a Claude
    summary. Use this after significant docstring or comment improvements where the old
    summary may be stale. Without force, already-summarized functions are skipped.

    project_id: the project to enrich (must match the value used in index_project).
    limit: max functions to process in this call. Call repeatedly to enrich all functions.
    """
    svcs = await _get_services()
    await check_permission(get_current_user(), project_id, "write", svcs.db)
    user = get_current_user()
    user_id = user["id"] if user else "anon"
    try:
        job = _check_and_enqueue(user_id, run_enrich_summaries, project_id, limit, force, job_timeout=7200)
    except RuntimeError:
        return json.dumps({"status": "rate_limited"})
    return json.dumps({"job_id": job.id, "status": "queued"})


_PATTERN_CONTRACT_NOTICE = (
    "PATTERN CONTRACT: If new functions are added to this subsystem, "
    "add their IDs to this contract's function_ids to maintain coverage."
)


def _fmt_contracts(contracts: list[dict]) -> list[dict]:
    """Format active contracts for inline injection into tool responses."""
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


async def _contracts_for_name(db, function_name: str, project_id: str) -> list[dict]:
    """Resolve a function name to its full ID and return applicable active contracts."""
    hits = await db.find_node_by_name(function_name, project_id or None)
    if not hits:
        return []
    node = hits[0]
    return await db.get_contracts_for_function(
        node["id"], node.get("project_id") or project_id
    )


@mcp.tool()
async def get_callers(function_name: str, project_id: str = "") -> str:
    """
    [EXECUTION TOOL — accepts a symbol name]

    Return all functions that call the specified function. Accepts a bare name,
    a qualified name (module.func), or a full id (module.Class.method).

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    from .guidance import compute_callers_guidance
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.db.get_callers(function_name, project_id or None)
    contracts = await _contracts_for_name(svcs.db, function_name, project_id)
    out: dict = {"callers": results, "_guidance": compute_callers_guidance(results, function_name)}
    if contracts:
        out["applicable_contracts"] = _fmt_contracts(contracts)
    return json.dumps(out)


@mcp.tool()
async def get_callees(function_name: str, project_id: str = "") -> str:
    """
    [EXECUTION TOOL — accepts a symbol name]

    Return all functions called by the specified function.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    from .guidance import compute_callees_guidance
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.db.get_callees(function_name, project_id or None)
    contracts = await _contracts_for_name(svcs.db, function_name, project_id)
    out: dict = {"callees": results, "_guidance": compute_callees_guidance(results, function_name)}
    if contracts:
        out["applicable_contracts"] = _fmt_contracts(contracts)
    return json.dumps(out)


@mcp.tool()
async def get_impact_radius(
    function_name: str, depth: int = 2, project_id: str = ""
) -> str:
    """
    [EXECUTION TOOL — accepts a symbol name]

    BFS traversal outward from function_name up to `depth` levels.
    Returns the set of functions that would be impacted by a change to
    function_name, annotated with their distance from the origin.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.db.get_impact_radius(function_name, depth, project_id or None)
    contracts = await _contracts_for_name(svcs.db, function_name, project_id)
    out: dict = {"impact_radius": results}
    if contracts:
        out["applicable_contracts"] = _fmt_contracts(contracts)
    return json.dumps(out)


@mcp.tool()
async def list_external_dependencies(project_id: str) -> str:
    """
    [DISCOVERY TOOL — accepts a project_id]

    Return all external library symbols called by this project, grouped by
    library. Each entry includes the symbol name, its call signature, and
    caller_count — how many internal functions reference it.

    Useful for dependency audits, migration planning, and understanding which
    parts of the codebase couple to a given library.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.db.list_external_dependencies(project_id)
    if not results:
        return json.dumps({
            "results": [],
            "_guidance": "No external dependency data found. This tool requires SCIP augmentation — "
                         "run index_scip (or index_project with scip-python installed) to populate "
                         "external symbol tracking for this project.",
        })
    return json.dumps(results)


@mcp.tool()
async def get_dependency_fingerprint(project_id: str) -> str:
    """
    [DISCOVERY TOOL — accepts a project_id]

    Return the latest dependency fingerprint for a project: all external library
    symbols in use grouped by library, plus a diff from the previous fingerprint.

    The diff is the failure-correlation signal:
    - removed_symbols: symbols present before but gone now — most likely cause
      of runtime ImportError or AttributeError after a dependency change.
    - changed_symbols: signatures that shifted — potential breaking API changes.
    - added_symbols: new external dependencies introduced since the last index.

    The fingerprint is also written to <project>/.phronosis/dependency-fingerprint.json
    on every index_project run, making dependency changes visible in git diff.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    row = await svcs.db.get_latest_dependency_fingerprint(project_id)
    if not row:
        return json.dumps({"status": "no fingerprint", "project_id": project_id,
                           "hint": "Run index_project to capture the first fingerprint."})
    return json.dumps(fingerprint_payload(row))


@mcp.tool()
async def list_dependency_fingerprint_history(
    project_id: str, limit: int = 50
) -> str:
    """
    [DISCOVERY TOOL — accepts a project_id]

    Return a summary of all dependency fingerprint snapshots for this project,
    newest first. Each row shows the fingerprint hash, when it was captured, and
    counts of removed / added / changed symbols versus the previous snapshot.

    Removed symbols are listed by ID when present — they are the primary signal
    for runtime failures caused by dependency changes. Use the fingerprint_id
    from a row of interest with get_dependency_fingerprint_at() to retrieve the
    full snapshot and diff for that point in time.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.db.list_dependency_fingerprint_history(project_id, limit)
    return json.dumps(results)


@mcp.tool()
async def get_dependency_fingerprint_at(fingerprint_id: str) -> str:
    """
    [DISCOVERY TOOL — accepts a fingerprint_id from list_dependency_fingerprint_history]

    Return the full dependency snapshot and diff for a specific point in time.
    Use this to investigate what the external dependency surface looked like at
    the time of a past incident — compare against a known-good fingerprint to
    identify which symbols were removed or changed between two index runs.
    """
    svcs = await _get_services()
    await _check_read_access("", svcs.db)
    row = await svcs.db.get_dependency_fingerprint_by_id(fingerprint_id)
    if not row:
        return json.dumps({"status": "not found", "fingerprint_id": fingerprint_id})
    return json.dumps(fingerprint_payload(row))


@mcp.tool()
async def get_library_dependents(library_name: str, project_id: str) -> str:
    """
    [EXECUTION TOOL — accepts a library name and project_id]

    Return all internal functions that call any symbol in the given external
    library. Answers: "if library X changes, which of my functions are exposed?"

    Each result includes the internal function's name, file, module, signature,
    and call_count — how many distinct library symbols it calls.

    Useful for migration planning ("what do I need to touch to replace requests
    with httpx?") and for scoping the blast radius of a dependency upgrade.

    library_name: bare library name, e.g. "requests", "numpy", "fastapi".
    project_id: required — scoped to a single project.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.db.get_library_dependents(library_name, project_id)
    if not results:
        return json.dumps({
            "results": [],
            "_guidance": f"No internal functions found that call '{library_name}'. "
                         "If this library is used, external dependency data may not be populated — "
                         "run index_scip (or index_project with scip-python installed) to enable "
                         "external symbol tracking.",
        })
    return json.dumps(results)


@mcp.tool()
async def compare_dependency_fingerprints(
    fingerprint_id_a: str, fingerprint_id_b: str
) -> str:
    """
    [EXECUTION TOOL — accepts two fingerprint IDs]

    Diff two dependency fingerprints from any point in history. Returns the
    same diff structure as get_dependency_fingerprint but between arbitrary
    snapshots rather than against the previous one.

    Use this to answer: "what exactly changed between the last known-good deploy
    and the deploy where the failure appeared?"

    Get fingerprint IDs from list_dependency_fingerprint_history. The diff is
    computed as (a → b): symbols in b that weren't in a are "added", symbols in
    a that aren't in b are "removed".
    """
    svcs = await _get_services()
    await _check_read_access("", svcs.db)
    row_a = await svcs.db.get_dependency_fingerprint_by_id(fingerprint_id_a)
    row_b = await svcs.db.get_dependency_fingerprint_by_id(fingerprint_id_b)
    if not row_a:
        return json.dumps({"status": "not found", "fingerprint_id": fingerprint_id_a})
    if not row_b:
        return json.dumps({"status": "not found", "fingerprint_id": fingerprint_id_b})
    fp_a = fingerprint_from_row(row_a)
    fp_b = fingerprint_from_row(row_b)
    diff = DependencyFingerprinter().diff(fp_a, fp_b)
    return json.dumps({
        "from": {"id": fingerprint_id_a, "captured_at": fp_a.captured_at, "hash": fp_a.fingerprint_hash},
        "to":   {"id": fingerprint_id_b, "captured_at": fp_b.captured_at, "hash": fp_b.fingerprint_hash},
        "diff": diff.to_dict(),
        "has_changes": diff.has_changes,
    })


@mcp.tool()
async def check_dependency(library_name: str, project_id: str) -> str:
    """
    [DISCOVERY TOOL — accepts a library name and project_id]

    Full dependency health check for a single library. Returns in one call:
    - version: installed version from the latest fingerprint (or "unknown")
    - symbols: all external symbols Phronosis has seen from this library
    - dependents: internal functions that call into this library, with call_count
    - recent_changes: version or symbol changes from the latest fingerprint diff

    This is the single-call answer to "is this dependency safe?" — combine with
    list_dependency_fingerprint_history to see the full change history.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    dependents = await svcs.db.get_library_dependents(library_name, project_id)
    fp_row = await svcs.db.get_latest_dependency_fingerprint(project_id)
    payload = fingerprint_payload(fp_row) if fp_row else None
    result = svcs.checker.health_envelope(library_name, dependents, payload)
    result["project_id"] = project_id
    return json.dumps(result)


# ── Semantic embedding tool ───────────────────────────────────────────────────

@mcp.tool()
async def query_similar_functions(
    snippet: str, top_k: int = 10, project_id: str = ""
) -> str:
    """
    [DISCOVERY TOOL — accepts natural language or a code snippet]

    Return the top-k functions semantically similar to the provided snippet.
    Surfaces parallel implementations, related modules, and similar patterns
    that would not appear in a grep or call trace.

    Use this when you do not yet know a function name — describe what you are
    looking for in plain English and this tool finds it.  Once you have the
    function name, switch to get_function_context for the full picture.

    project_id: limit results to a specific project. If omitted, searches across all projects.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.embeddings.query_similar(snippet, top_k, project_id or None)
    if project_id and results:
        from .guidance import compute_guidance
        guidance = await compute_guidance(results, svcs.db, project_id)
        return json.dumps({"results": results, "_guidance": guidance.to_dict()})
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
    await check_permission(get_current_user(), project_id, "write", svcs.db)
    result = await svcs.decisions.log_decision(
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
    [EXECUTION TOOL — accepts a symbol name]

    Return the full decision lineage for a function — every Architectural,
    Design, Implementation, and Patch decision linked to it, in chronological
    order. Call this before touching any function you did not write.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    from .guidance import compute_decision_guidance
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.decisions.get_decision_history(
        function_name, project_id or None
    )
    return json.dumps({
        "decisions": results,
        "_guidance": compute_decision_guidance(results, function_name, project_id),
    })


@mcp.tool()
async def query_decisions(
    query_text: str, project_id: str = ""
) -> str:
    """
    [DISCOVERY TOOL — accepts natural language]

    Semantic search over the full decision corpus. Useful for finding prior
    decisions that are relevant to a new change, even if they are not linked
    to the specific function you are editing.  Input is plain English intent,
    not a symbol name.

    project_id: limit results to a specific project. If omitted, searches all projects.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    results = await svcs.decisions.query_decisions(
        query_text, project_id=project_id or None
    )
    return json.dumps(results)


# ── Client setup tool ────────────────────────────────────────────────────────

@mcp.tool()
async def setup_phronosis_client(
    project_root: str,
    project_id: str = "",
    claude_home: str = "",
    install_git_hook: bool = True,
    server_url: str = "",
) -> str:
    """
    First-time setup for a new Phronosis-integrated project. Call this once after
    connecting to the MCP server. Execute the returned script with Bash to
    complete setup — no other manual steps required.

    Installs on the client machine:
    - Pre-edit hook (~/.claude/hooks/phronosis-suggest.py): fires before every Edit,
      warns when editing chokepoints or risk-surface functions, and nudges toward
      Phronosis tools on grep/Read.
    - Claude Code settings.json: adds Bash/Read/Edit PreToolUse hook matchers.
    - Project CLAUDE.md: three-tier workflow, pre-edit gate, multi-agent context.
    - Memory files: behavioral guidance that persists across sessions.
    - Post-commit git hook: re-indexes changed files + logs decisions on commit.

    project_root: absolute path to the project directory on the client machine
    project_id: slug for this project (default: derived from project_root basename)
    claude_home: path to ~/.claude directory (default: ~/.claude)
    install_git_hook: whether to install the post-commit git hook (default: true)
    server_url: the URL you used to connect to this MCP server (e.g. "http://100.71.88.106:3004").
                Pass this so generated hooks and CLAUDE.md point at the right server.
                Defaults to the PHRONOSIS_URL env var on the server, then http://localhost:3004.

    Usage:
        result = setup_phronosis_client("/Users/name/myproject", server_url="http://100.71.88.106:3004")
        # Then run: exec(result["setup_script"])  or  Bash(result["run_command"])
    """
    if not project_root:
        return json.dumps({"status": "error", "detail": "project_root is required"})

    pid = project_id or _derive_project_id(project_root)
    home = claude_home or _default_claude_home()
    server_url = server_url or os.getenv("PHRONOSIS_URL", "http://localhost:3004")

    # Load post-commit hook content from the scripts directory.
    scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
    post_commit_path = os.path.join(scripts_dir, "post-commit.sh")
    try:
        with open(post_commit_path) as f:
            post_commit = f.read().replace(
                'PHRONOSIS_URL="${PHRONOSIS_URL:-http://localhost:3004}"',
                f'PHRONOSIS_URL="${{PHRONOSIS_URL:-{server_url}}}"',
            )
    except FileNotFoundError:
        post_commit = f'#!/usr/bin/env bash\n# post-commit hook\nexport PHRONOSIS_URL="{server_url}"\n'
        install_git_hook = False

    script = generate_setup_script(
        project_root=project_root,
        phronosis_url=server_url,
        project_id=pid,
        claude_home=home,
        install_git_hook=install_git_hook,
        post_commit_content=post_commit,
    )

    return json.dumps({
        "status": "ready",
        "project_id": pid,
        "phronosis_url": server_url,
        "run_command": "python3 << 'Phronosis_SETUP'\n" + script + "\nPhronosis_SETUP",
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
    [DISCOVERY TOOL — accepts a project_id, returns full architectural picture]

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
    await _check_read_access(project_id, svcs.db)
    result = await svcs.arch.get_project_home(project_id, max_age_seconds=300)
    return json.dumps(result)


# ── Contract tools ────────────────────────────────────────────────────────────

@mcp.tool()
async def create_contract(
    title: str,
    natural_language: str,
    project_ids: list[str] | None = None,
    function_ids: list[str] | None = None,
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
    function_ids: optional list of function IDs to scope this contract to a specific
        subsystem or pattern (e.g. all functions forming an Observer). When set,
        check_contracts only evaluates those functions instead of the whole project.
    """
    svcs = await _get_services()
    for _pid in (project_ids or []):
        await check_permission(get_current_user(), _pid, "write", svcs.db)
    result = await svcs.contracts.generate_draft(
        project_ids=project_ids or [],
        title=title,
        natural_language=natural_language,
        function_ids=function_ids,
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
    user = get_current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    _contract = await svcs.db.get_contract(contract_id)
    if _contract:
        for _pid in (_contract.get("project_ids") or []):
            await check_permission(user, _pid, "write", svcs.db)
    result = await svcs.contracts.approve(contract_id)
    return json.dumps(result)


@mcp.tool()
async def list_contracts(project_id: str = "") -> str:
    """
    List all contracts with their examples and status.

    project_id: filter to contracts that apply to this project. If omitted,
    returns all contracts across all projects.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    result = await svcs.contracts.list_contracts(project_id or None)
    return json.dumps(result)


@mcp.tool()
async def check_contracts(project_id: str, semantic: bool = False) -> str:
    """
    Run all active contracts against the current call graph for a project.

    Returns a list of violations — structural (call graph traversal up to depth 2,
    catching one-wrapper bypasses) and optionally semantic (embedding similarity
    against violation examples).

    semantic=False (default): structural checks only — fast, suitable for CI.
    semantic=True: also runs embedding checks against every project function.
    Expensive on large projects — use on small codebases or focused subsets.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    result = await svcs.contracts.check_project(project_id, semantic=semantic)
    return json.dumps(result)


# delete_contract is intentionally NOT exposed as an MCP tool OR HTTP endpoint.
# An agent could delete a contract to bypass enforcement — this is the
# primary adversarial failure vector for the contracts system.
# Use deactivate (POST .../deactivate) to retire a contract; it preserves
# the audit trail and prevents silent removal by agents or unauthenticated callers.


# ── Performance detectors ─────────────────────────────────────────────────────

@mcp.tool()
async def check_performance(project_id: str, exclude_test_files: bool = True) -> str:
    """
    Surface potential performance concerns in an indexed project.

    Runs three detectors:
    - correlated_join_aggregate: SQL queries that JOIN two tables sharing a
      parent key and then aggregate with COUNT — produces a row cross-product.
      This is the class of bug that caused list_projects to hang indefinitely.
    - n_plus_one: functions that contain a loop and call a DB-accessing
      function inside it — O(n) queries instead of O(1).
    - quadratic_expansion: functions whose embeddings cluster near cross-product
      semantics and either call another such function (silent O(n²) composition
      with no visible loop) or are called inside a loop (O(n) × O(m) expansion).
      Requires function embeddings to be indexed.

    Findings already acknowledged via dismiss_performance_concern are returned
    with status="acknowledged" so you know they exist but chose to accept them.
    New findings are returned with status="new".

    Respond to new findings by either fixing them or calling
    dismiss_performance_concern with the reason the pattern is intentional.
    """
    from .performance import check_performance as _check
    from .guidance import compute_performance_guidance
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    findings = await _check(svcs.db, project_id, embeddings=svcs.embeddings,
                            exclude_test_files=exclude_test_files)
    return json.dumps({
        "project_id": project_id,
        "total": len(findings),
        "new": sum(1 for f in findings if not f.suppressed),
        "acknowledged": sum(1 for f in findings if f.suppressed),
        "findings": [f.to_dict() for f in findings],
        "_guidance": compute_performance_guidance(findings),
    }, indent=2)


@mcp.tool()
async def validate_proposed_code(
    code: str,
    target_file: str,
    project_id: str,
) -> str:
    """
    Pre-flight conformance check for code before writing it to disk.

    Parses the proposed code in-memory, compares it against the indexed module's
    naming and async conventions, checks active contracts, and runs performance
    detectors on function bodies.

    Returns a conformance_score (0–1) with specific deviations and examples
    from the existing module so you can see exactly what convention to follow.

    code:        the proposed source code string (full function definitions)
    target_file: path of the file being written (determines language + module context)
    project_id:  which project's conventions and contracts to check against
    """
    from .validate import validate_proposed_code as _validate
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    result = await _validate(code, target_file, project_id, svcs.db)
    return json.dumps(result.to_dict(), indent=2)


@mcp.tool()
async def preflight_architecture(project_id: str) -> str:
    """
    Gather structural signals that focus an architectural review on high-value areas.

    Returns four categories of signal as a Markdown brief:

    1. Coupling hotspots — internal functions with high fan-in × fan-out.
       High score = structurally central. Apply the deletion test to decide
       whether the function is a load-bearing seam or an accidental hub.

    2. External dependency scatter — libraries used directly from 3+ files,
       suggesting a missing adapter layer. One adapter = hypothetical seam;
       two adapters (prod + test) = real seam worth introducing.

    3. Duplication clusters — semantically similar functions spread across
       3+ files, suggesting a concept that should be a single deep module.
       Requires function embeddings to be indexed.

    4. Performance → structural cause — maps check_performance findings to
       the missing abstraction that would eliminate each pattern structurally.

    Feed the output directly to the improve-codebase-architecture skill's
    Explore step so it knows where to look rather than walking blind.
    """
    from .performance import check_performance as _check_perf
    from .architecture_preflight import run_preflight

    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    findings = await _check_perf(svcs.db, project_id, embeddings=svcs.embeddings)
    preflight = await run_preflight(
        svcs.db,
        project_id,
        performance_findings=findings,
        embeddings=svcs.embeddings,
    )
    return preflight.to_brief()


@mcp.tool()
async def dismiss_performance_concern(
    project_id: str,
    function_id: str,
    reason: str,
) -> str:
    """
    Acknowledge a performance finding as intentional so it is not re-surfaced.

    Use this when check_performance flags something you have already reviewed
    and decided is acceptable — for example, a JOIN that looks like a
    cross-product but is bounded by a WHERE clause you know limits the rows.

    The reason is stored as a Performance decision in decision memory, linked
    to the function. Future check_performance calls will show it with
    status="acknowledged" and your reason, so the context is preserved.

    function_id: the id field from the check_performance finding
    reason: why this pattern is intentional or acceptable
    """
    svcs = await _get_services()
    await check_permission(get_current_user(), project_id, "write", svcs.db)
    result = await svcs.decisions.log_decision(
        type="Performance",
        description=reason,
        rejected_alternatives="",
        trigger=f"dismissed via check_performance on project {project_id}",
        linked_function_ids=[function_id],
        parent_decision_id=None,
        project_id=project_id,
    )
    return json.dumps({"status": "acknowledged", "decision_id": result.get("id")})


# ── SOLID detectors ───────────────────────────────────────────────────────────

@mcp.tool()
async def check_solid_principles(project_id: str) -> str:
    """
    Surface SOLID principle violations in an indexed project.

    Runs three structural detectors — no embeddings or LLM calls required:

    - SRP (Single Responsibility): functions whose callees span 3+ unrelated
      subsystems have multiple independent reasons to change.

    - OCP (Open/Closed): functions that isinstance-dispatch on 3+ concrete
      types must be modified every time a new type is added — the opposite of
      "closed for modification." Consider polymorphism or a handler registry.

    - DIP (Dependency Inversion): non-infrastructure functions that directly
      call raw DB-layer functions across a subsystem boundary skip the
      abstraction layer. Business logic should depend on an interface, not
      concrete storage internals.

    Findings already acknowledged via dismiss_solid_concern are returned with
    status="acknowledged". New findings are returned with status="new".

    Respond to new findings by either refactoring or calling
    dismiss_solid_concern with the reason the pattern is intentional.
    """
    from .solid import check_solid as _check
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    findings = await _check(svcs.db, project_id)
    by_principle: dict[str, int] = {}
    for f in findings:
        if not f.suppressed:
            by_principle[f.principle] = by_principle.get(f.principle, 0) + 1
    return json.dumps({
        "project_id": project_id,
        "total": len(findings),
        "new": sum(1 for f in findings if not f.suppressed),
        "acknowledged": sum(1 for f in findings if f.suppressed),
        "by_principle": by_principle,
        "findings": [f.to_dict() for f in findings],
    }, indent=2)


@mcp.tool()
async def dismiss_solid_concern(
    project_id: str,
    function_id: str,
    reason: str,
) -> str:
    """
    Acknowledge a SOLID finding as intentional so it is not re-surfaced.

    Use this when check_solid_principles flags something you have reviewed and
    accepted — for example, an orchestrator function that intentionally
    coordinates multiple subsystems and its cross-subsystem calls are by design.

    The reason is stored as a SOLID decision in decision memory, linked to the
    function. Future check_solid_principles calls will show it with
    status="acknowledged" and your reason.

    function_id: the id field from the check_solid_principles finding
    reason: why this pattern is intentional or acceptable
    """
    svcs = await _get_services()
    await check_permission(get_current_user(), project_id, "write", svcs.db)
    result = await svcs.decisions.log_decision(
        type="SOLID",
        description=reason,
        rejected_alternatives="",
        trigger=f"dismissed via check_solid_principles on project {project_id}",
        linked_function_ids=[function_id],
        parent_decision_id=None,
        project_id=project_id,
    )
    return json.dumps({"status": "acknowledged", "decision_id": result.get("id")})


@mcp.tool()
async def index_schema_objects(project_id: str, include_db_tables: bool = False) -> str:
    """
    Extract and embed schema objects for a project, building the object
    embedding layer used by check_performance to score N+1 findings.

    Two object types are extracted:
    - Python classes from the call graph (all projects)
    - Postgres DB tables with FK relationships and cardinality (set
      include_db_tables=True for projects that use this Phronosis database)

    Each object is embedded as a structured description capturing what it
    represents, what it relates to, and its cardinality class (LOW / MEDIUM /
    HIGH / UNBOUNDED). check_performance uses these embeddings to distinguish
    high-cardinality correlated access patterns (likely real performance issues)
    from low-cardinality intentional loops (likely fine).

    Run this after index_project to enable object-embedding-enhanced scoring.
    """
    from .schema_objects import index_schema_objects as _index
    svcs = await _get_services()
    await check_permission(get_current_user(), project_id, "write", svcs.db)
    result = await _index(
        svcs.db, svcs.embeddings, project_id,
        include_db_tables=include_db_tables,
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
        await _require_http_read(request, svcs.db, project_id)
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
        _user = await _require_valid_key(request, svcs.db)
        _accessible = await svcs.db.get_accessible_project_ids(_user["id"])
        _all = await svcs.db.list_projects()
        projects = [p for p in _all if p["id"] in _accessible]
        return JSONResponse({"projects": projects})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)


# ── HTTP auth helper (write endpoints only) ───────────────────────────────────────

async def _require_valid_key(request: Request, db: "CallGraphDB") -> dict:
    """Validate X-API-Key header for HTTP write endpoints. Raises 401 if missing/invalid."""
    key = request.headers.get("X-API-Key")
    if not key:
        raise HTTPException(status_code=401, detail="Authentication required — include X-API-Key header")
    user = await db.get_user_by_key(key)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")
    return user


async def _check_read_access(project_id: str, db: "CallGraphDB") -> None:
    """Require a valid API key for all MCP reads; additionally enforce project access when project_id is given."""
    user = get_current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    if project_id:
        await check_permission(user, project_id, "read", db)


async def _require_http_read(request: Request, db: "CallGraphDB", project_id: str = "") -> dict:
    """Validate X-API-Key for HTTP GET/read endpoints; check project read access when project_id given."""
    user = await _require_valid_key(request, db)
    if project_id:
        await check_permission(user, project_id, "read", db)
    return user


# ── Bulk index HTTP endpoint (used by phronosis-import slash command) ──────────────

@mcp.custom_route("/api/index-bulk", methods=["POST"])
async def http_index_bulk(request: Request) -> JSONResponse:
    """
    POST /api/index-bulk
    Body: {"project_root": "/abs/path", "project_id": "myapp",
           "files": {"abs/path/file.py": "<content>", ...}}
    Indexes a full project from file contents supplied by the caller — no server-side
    file I/O required. Used when the project lives on a different machine than the
    Phronosis server (e.g. the Claude Code workspace vs TheHive).
    project_id is derived from project_root's basename if not provided.
    """
    try:
        svcs = await _get_services()
        _user = await _require_valid_key(request, svcs.db)
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
        await _require_valid_key(request, svcs.db)
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
        await _require_valid_key(request, svcs.db)
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
        await _require_http_read(request, svcs.db, project_id)
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
        _user = await _require_valid_key(request, svcs.db)
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
        _user = await _require_valid_key(request, svcs.db)
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


@mcp.custom_route("/api/contracts/{contract_id}/approve", methods=["POST"])
async def http_approve_contract(request: Request) -> JSONResponse:
    """POST /api/contracts/{id}/approve"""
    try:
        contract_id = request.path_params["contract_id"]
        svcs = await _get_services()
        _user = await _require_valid_key(request, svcs.db)
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
        _user = await _require_valid_key(request, svcs.db)
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
        await _require_http_read(request, svcs.db, project_id)
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
        await _require_http_read(request, svcs.db, project_id)
        violations = await svcs.db.list_violations(project_id or None)
        return JSONResponse({"violations": violations})
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse({"status": "error", "detail": str(exc)}, status_code=500)




# ── Unified Context Pipeline ───────────────────────────────────────────────────

@mcp.tool()
async def get_function_context(
    query: str,
    project_id: str = "",
    depth: int = 2,
) -> str:
    """
    [PRIMARY ENTRY POINT — accepts natural language OR a symbol name]

    One-call unified context pipeline. Runs semantic search to find the right
    function, then chains call graph traversal and decision memory into a single
    enriched payload. The agent never needs to call individual layer tools.

    Pipeline (Step 1: semantic search → Step 2: call graph → Step 3: decisions).

    query: natural language ("state transition function") OR a known symbol name.
           Semantic search runs first; symbol lookup is the fallback.
    project_id: limit to a specific project. If omitted, searches all projects.
    depth: call graph traversal depth (default 2).
    """
    import asyncio as _asyncio
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    db = svcs.db
    decisions = svcs.decisions
    embeddings = svcs.embeddings

    pid = project_id or None

    # Step 1: Semantic search — primary path for natural language queries.
    # Embed the query and find the best matching function via KNN vector search.
    # Fall back to exact/fuzzy symbol lookup when embeddings aren't available.
    node = None
    semantic_hits = await embeddings.query_similar(query, top_k=5, project_id=pid)
    if semantic_hits:
        top_id = semantic_hits[0].get("id", "")
        name_hits = await db.find_node_by_name(top_id, pid)
        if name_hits:
            node = name_hits[0]

    if node is None:
        # Fallback: exact/fuzzy symbol name lookup (handles precise symbol queries
        # and projects with no embeddings yet)
        name_hits = await db.find_node_by_name(query, pid)
        if name_hits:
            node = name_hits[0]

    if node is None:
        return json.dumps({
            "error": (
                f"No function found matching '{query}'. "
                "Try a more specific description or call index_project first."
            )
        })

    node_id = node["id"]
    node_project = node.get("project_id", project_id)

    # Step 2: fan out — all queries run concurrently
    callers_task = _asyncio.create_task(db.get_callers(node_id, node_project))
    callees_task = _asyncio.create_task(db.get_callees(node_id, node_project))
    impact_task = _asyncio.create_task(db.get_impact_radius(node_id, depth, node_project))
    history_task = _asyncio.create_task(decisions.get_decision_history(node_id, node_project))
    similar_task = _asyncio.create_task(
        embeddings.query_similar(
            node.get("signature", "") + " " + node.get("docstring", ""),
            top_k=6,
            project_id=node_project,
        )
    )
    contracts_task = _asyncio.create_task(
        db.get_contracts_for_function(node_id, node_project)
    )

    callers, callees, impact, history, similar, contracts = await _asyncio.gather(
        callers_task, callees_task, impact_task, history_task, similar_task, contracts_task,
        return_exceptions=True,
    )

    def _safe(val, default):
        return default if isinstance(val, Exception) else val

    # Filter self from similar
    similar_clean = [
        s for s in _safe(similar, [])
        if s.get("id") != node_id
    ][:5]

    param_names = node.get("parameter_names")
    if isinstance(param_names, str):
        import json as _j
        try:
            param_names = _j.loads(param_names)
        except Exception:
            param_names = []

    return json.dumps({
        "node": {
            "id": node_id,
            "name": node.get("name"),
            "file": node.get("file"),
            "module": node.get("module"),
            "type": node.get("type"),
            "signature": node.get("signature"),
            "docstring": node.get("docstring"),
            "summary": node.get("summary"),
            "start_line": node.get("start_line", 0),
            "end_line": node.get("end_line", 0),
            "return_type": node.get("return_type", ""),
            "is_async": bool(node.get("is_async", 0)),
            "parameter_names": param_names or [],
            "enclosing_class": node.get("enclosing_class", ""),
        },
        "callers": _safe(callers, []),
        "callees": _safe(callees, []),
        "impact_radius": _safe(impact, []),
        "decision_history": _safe(history, []),
        "similar_functions": similar_clean,
        "applicable_contracts": _fmt_contracts(_safe(contracts, [])),
    })



@mcp.tool()
async def find_dependents(
    symbol: str,
    project_id: str = "",
    depth: int = 3,
) -> str:
    """
    [EXECUTION TOOL — accepts a symbol name]

    Return everything that depends on the given symbol — all callers and their
    callers, up to `depth` levels. Answers the question: "if I change this, what
    breaks?"

    This is the task-oriented complement to get_function_context. Use it when you
    already know the symbol and want to understand the blast radius before editing.

    symbol: function name, qualified name, or full ID.
    project_id: limit to a specific project. If omitted, searches all projects.
    depth: how many levels of dependents to traverse (default 3).

    Returns a list of dependent symbols with their distance from the origin,
    file location, and signature — ordered nearest-first.
    """
    svcs = await _get_services()
    await _check_read_access(project_id, svcs.db)
    result = await svcs.db.get_impact_radius(symbol, depth, project_id or None)
    return json.dumps(result)


# ── LSIF / SCIP ingestion ─────────────────────────────────────────────────────

@mcp.tool()
async def index_lsif(path: str, project_id: str = "") -> str:
    """
    Ingest a pre-built LSIF (Language Server Index Format) index file into Phronosis.

    LSIF files are produced by CI/CD indexers like lsif-py, lsif-tsc, lsif-java,
    rust-analyzer, and others. They provide symbol definitions with hover
    documentation and cross-reference data for any language without needing a
    live tree-sitter parser.

    path: absolute path to the .lsif NDJSON file on the Phronosis server filesystem.
    project_id: namespace for the indexed symbols (defaults to the lsif filename stem).

    Use this when:
    - Indexing a language not yet supported by tree-sitter
    - Ingesting multi-repo or monorepo indexes built in CI
    - Bootstrapping a project from an existing Sourcegraph or GitHub index export
    """
    svcs = await _get_services()
    await check_permission(get_current_user(), project_id or "default", "write", svcs.db)
    result = await svcs.indexer.index_lsif(path, project_id=project_id)
    return json.dumps(result)


@mcp.tool()
async def index_scip(path: str, project_id: str = "") -> str:
    """
    Ingest a pre-built SCIP (Sourcegraph Code Intelligence Protocol) JSON index
    into Phronosis.

    SCIP is the successor to LSIF — produced by scip-python, scip-typescript,
    scip-java, rust-analyzer, and others. The JSON form is accepted here (produced
    by `scip convert --to json` or directly by some indexers).

    SCIP provides explicit symbol documentation and relationship data, yielding
    both FunctionNode records and call edges from the relationships section.

    path: absolute path to the .scip.json file on the Phronosis server filesystem.
    project_id: namespace for the indexed symbols (defaults to the filename stem).
    """
    svcs = await _get_services()
    await check_permission(get_current_user(), project_id or "default", "write", svcs.db)
    result = await svcs.indexer.index_scip(path, project_id=project_id)
    return json.dumps(result)




@mcp.custom_route("/api/reembed/{project_id}", methods=["POST"])
async def http_reembed_project(request: Request) -> JSONResponse:
    """POST /api/reembed/{project_id} — force re-embed all functions for a project."""
    try:
        project_id = request.path_params["project_id"]
        svcs = await _get_services()
        _user = await _require_valid_key(request, svcs.db)
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
        user = await _require_valid_key(request, svcs.db)
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="0.0.0.0", port=3004, stateless_http=True)
