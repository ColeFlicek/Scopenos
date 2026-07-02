"""Indexing tools: project indexing, incremental updates, embedding management."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from fastmcp import FastMCP

from ..auth import get_current_user, check_permission
from ..indexer import _derive_project_id
from ..jobs import run_enrich_summaries, run_reembed_project
from ._shared import check_and_enqueue
from . import _shared as _tools_shared


def register(mcp: FastMCP, _unused_get_services: Callable = None) -> tuple:

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
        import os
        from ..indexer import estimate_project
        if not os.path.exists(path):
            return json.dumps({"error": f"Path not found: {path}"})
        return json.dumps(estimate_project(path))

    @mcp.tool()
    async def index_project(
        files: dict[str, str],
        project_id: str,
        project_root: str = "",
        branch: str = "",
        head_commit: str = "",
    ) -> str:
        """
        Index a project by sending file contents from the client.

        files: dict of {path: file_content} for every source file to index.
               Call in batches of ~100 files for large projects — repeated calls
               accumulate into the same index.
        project_id: stable slug for this project (e.g. "myapp"). Use the same
                    value on every call.
        project_root: local root path used only for deriving module names
                      (e.g. "/workspace/myapp"). Does not need to exist on the server.
        branch: current git branch. Run `git rev-parse --abbrev-ref HEAD` on the client.
        head_commit: current HEAD SHA. Run `git rev-parse HEAD` on the client.

        Use index_changes for subsequent per-session updates after the initial index.
        """
        svcs = await _tools_shared.get_services()
        user = get_current_user()
        if user and not await svcs.db.has_any_owner(project_id):
            await svcs.db.grant_project_access(user["id"], project_id, "owner")
        await check_permission(user, project_id, "write", svcs.db)
        result = await svcs.indexer.index_changes(
            list(files.keys()), files,
            project_root=project_root, project_id=project_id,
            branch=branch, head_commit=head_commit,
        )
        written_ids = result.pop("function_ids", [])
        if written_ids:
            pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
            violations = await svcs.contracts.check_functions(project_id, written_ids, pdb=pdb)
            result["contract_violations"] = violations
        else:
            result["contract_violations"] = []
        return json.dumps(result)

    @mcp.tool()
    async def index_changes(
        file_paths: list[str],
        file_contents: dict[str, str],
        project_root: str = "",
        project_id: str = "",
        branch: str = "",
        head_commit: str = "",
    ) -> str:
        """
        Incremental update for changed files. Pass the paths and current contents
        of modified files. Stale call graph edges and embeddings are dropped and
        replaced. Pass project_root (same value used in index_project) to ensure
        module IDs are consistent with the full index.

        project_id: must match the value used in index_project. If omitted,
        derived from project_root's last component.
        branch: current git branch. Run `git rev-parse --abbrev-ref HEAD` on the client.
        head_commit: current HEAD SHA. Run `git rev-parse HEAD` on the client.

        Contract violations detected in the written functions are returned inline
        under "contract_violations". An empty list means no active contracts fired.
        Fix or acknowledge violations before proceeding — writing code that violates
        an active contract is the primary adversarial failure vector for the system.
        """
        svcs = await _tools_shared.get_services()
        pid = project_id or (_derive_project_id(project_root) if project_root else "default")
        await check_permission(get_current_user(), pid, "write", svcs.db)
        result = await svcs.indexer.index_changes(
            file_paths, file_contents, project_root=project_root, project_id=project_id,
            branch=branch, head_commit=head_commit,
        )
        written_ids = result.pop("function_ids", [])
        pdb = await _tools_shared.resolve_project_db(pid, svcs.db)
        if written_ids:
            violations = await svcs.contracts.check_functions(pid, written_ids, pdb=pdb)
            result["contract_violations"] = violations
        else:
            result["contract_violations"] = []
        # Invalidate arch cache so next get_project_home sees fresh data.
        if hasattr(pdb, "_arch_service"):
            pdb._arch_service.invalidate(pid)
        return json.dumps(result)

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
        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), project_id, "write", svcs.db)
        user = get_current_user()
        user_id = user["id"] if user else "anon"
        try:
            job = check_and_enqueue(user_id, run_reembed_project, project_id, job_timeout=3600, db_url=svcs.db._dsn)
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
        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), project_id, "write", svcs.db)
        user = get_current_user()
        user_id = user["id"] if user else "anon"
        try:
            job = check_and_enqueue(user_id, run_enrich_summaries, project_id, limit, force, job_timeout=7200, db_url=svcs.db._dsn)
        except RuntimeError:
            return json.dumps({"status": "rate_limited"})
        return json.dumps({"job_id": job.id, "status": "queued"})

    @mcp.tool()
    async def fork_project(
        project_id: str,
        commit_ref: str,
        fork_id: str = "",
    ) -> str:
        """
        Fork a project at a specific git commit.

        Creates an isolated copy of the project's call graph at commit_ref.
        Unchanged functions are copied from the parent as-is; functions whose
        source changed between commit_ref and HEAD are re-parsed from the older
        source text so the fork reflects the codebase state at that commit.

        project_id: the project to fork (must already be indexed).
        commit_ref: any git ref (branch, tag, SHA) — resolved to a full SHA.
        fork_id: ID for the new fork project. If omitted, auto-generated as
                 "{project_id}_fork_{short_sha}".

        Returns JSON with fork_project_id, schema_name, and a delta summary
        (updated/deleted/unchanged function counts).
        """
        import subprocess
        from ..fork import create_fork

        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), project_id, "write", svcs.db)

        repo_root = await svcs.db.get_project_root(project_id)
        if not repo_root:
            return json.dumps({"error": f"Project {project_id!r} has no recorded root path"})

        try:
            full_sha = subprocess.check_output(
                ["git", "-C", repo_root, "rev-parse", commit_ref],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except subprocess.CalledProcessError:
            return json.dumps({"error": f"Cannot resolve commit_ref {commit_ref!r} in {repo_root}"})

        short_sha = full_sha[:7]
        resolved_fork_id = fork_id or f"{project_id}_fork_{short_sha}"

        user = get_current_user()
        user_id = user["id"] if user else ""

        try:
            result = await create_fork(
                parent_project_id=project_id,
                target_commit=full_sha,
                fork_project_id=resolved_fork_id,
                repo_path=repo_root,
                org_db=svcs.db,
                user_id=user_id,
            )
        except Exception as exc:
            return json.dumps({"error": str(exc)})

        return json.dumps(result)

    @mcp.tool()
    async def drop_fork(fork_project_id: str) -> str:
        """
        Drop a fork project — removes its schema and projects row.

        Refuses to drop non-fork projects to prevent accidental deletion of
        primary indexes. To delete a non-fork project, contact an administrator.

        fork_project_id: must match the fork_project_id returned by fork_project.
        """
        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), fork_project_id, "write", svcs.db)

        # Check that this is actually a fork before deleting
        is_fork = await svcs.db.get_project_is_fork(fork_project_id)

        if is_fork is None:
            return json.dumps({"error": f"Project {fork_project_id!r} not found"})
        if not is_fork:
            return json.dumps({
                "error": (
                    f"Project {fork_project_id!r} is not a fork. "
                    "drop_fork only deletes fork projects to prevent accidental data loss."
                )
            })

        result = await svcs.db.delete_project(fork_project_id)
        return json.dumps({"dropped": True, **result})

    return index_project, index_changes, enrich_summaries
