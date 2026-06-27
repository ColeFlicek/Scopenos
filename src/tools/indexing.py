"""Indexing tools: project indexing, incremental updates, embedding management."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from fastmcp import FastMCP

from ..auth import get_current_user, check_permission
from ..indexer import _derive_project_id
from ..jobs import run_index_project, run_enrich_summaries, run_reembed_project
from ._shared import check_and_enqueue
from . import _shared as _tools_shared


def register(mcp: FastMCP, _unused_get_services: Callable = None) -> tuple:

    @mcp.tool()
    async def warmup_pattern_prototypes() -> str:
        """
        [MAINTENANCE TOOL — no arguments]

        Pre-compute and persist prototype embedding vectors for all GoF pattern
        roles defined in pattern_prototypes.py. Each role's 10 descriptions are
        embedded and averaged into a centroid stored in the pattern_prototypes table.

        Safe to call multiple times — roles whose description_hash matches the
        stored row are skipped (already current). Only re-embeds when descriptions
        change.

        Returns a summary of which roles were computed vs. loaded from cache.
        """
        svcs = await _tools_shared.get_services()
        from ..pattern_prototypes import ROLE_DESCRIPTIONS, ensure_prototype, _description_hash

        computed, cached, failed = [], [], []
        for role in ROLE_DESCRIPTIONS:
            try:
                existing = await svcs.embeddings.get_prototype(role)
                if existing and existing.get("description_hash") == _description_hash(role):
                    cached.append(role)
                else:
                    await ensure_prototype(role, svcs.embeddings, svcs.embeddings)
                    computed.append(role)
            except Exception as exc:
                failed.append({"role": role, "error": str(exc)})

        return json.dumps({
            "computed": computed,
            "cached": cached,
            "failed": failed,
            "total_roles": len(ROLE_DESCRIPTIONS),
        })

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
        svcs = await _tools_shared.get_services()
        pid = project_id or Path(path).name or "default"
        await check_permission(get_current_user(), pid, "write", svcs.db)
        user = get_current_user()
        user_id = user["id"] if user else "anon"
        try:
            job = check_and_enqueue(user_id, run_index_project, path, pid, job_timeout=3600)
        except RuntimeError:
            return json.dumps({"status": "rate_limited"})
        import os
        hook_path = Path(path) / ".git" / "hooks" / "post-commit"
        hook_installed = hook_path.exists() and os.access(hook_path, os.X_OK)
        response: dict = {"job_id": job.id, "status": "queued"}
        if not hook_installed:
            response["hook_missing"] = True
            response["hook_warning"] = (
                "No executable post-commit hook found at .git/hooks/post-commit. "
                "Contract violations from direct git commits will not be detected. "
                "Install with: cp /path/to/scopenos/scripts/post-commit.sh "
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
        svcs = await _tools_shared.get_services()
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
            job = check_and_enqueue(user_id, run_reembed_project, project_id, job_timeout=3600)
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
            job = check_and_enqueue(user_id, run_enrich_summaries, project_id, limit, force, job_timeout=7200)
        except RuntimeError:
            return json.dumps({"status": "rate_limited"})
        return json.dumps({"job_id": job.id, "status": "queued"})

    @mcp.tool()
    async def index_lsif(path: str, project_id: str = "") -> str:
        """
        Ingest a pre-built LSIF (Language Server Index Format) index file into Scopenos.

        LSIF files are produced by CI/CD indexers like lsif-py, lsif-tsc, lsif-java,
        rust-analyzer, and others. They provide symbol definitions with hover
        documentation and cross-reference data for any language without needing a
        live tree-sitter parser.

        path: absolute path to the .lsif NDJSON file on the Scopenos server filesystem.
        project_id: namespace for the indexed symbols (defaults to the lsif filename stem).

        Use this when:
        - Indexing a language not yet supported by tree-sitter
        - Ingesting multi-repo or monorepo indexes built in CI
        - Bootstrapping a project from an existing Sourcegraph or GitHub index export
        """
        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), project_id or "default", "write", svcs.db)
        result = await svcs.indexer.index_lsif(path, project_id=project_id)
        return json.dumps(result)

    @mcp.tool()
    async def index_scip(path: str, project_id: str = "") -> str:
        """
        Ingest a pre-built SCIP (Sourcegraph Code Intelligence Protocol) JSON index
        into Scopenos.

        SCIP is the successor to LSIF — produced by scip-python, scip-typescript,
        scip-java, rust-analyzer, and others. The JSON form is accepted here (produced
        by `scip convert --to json` or directly by some indexers).

        SCIP provides explicit symbol documentation and relationship data, yielding
        both FunctionNode records and call edges from the relationships section.

        path: absolute path to the .scip.json file on the Scopenos server filesystem.
        project_id: namespace for the indexed symbols (defaults to the filename stem).
        """
        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), project_id or "default", "write", svcs.db)
        result = await svcs.indexer.index_scip(path, project_id=project_id)
        return json.dumps(result)

    @mcp.tool()
    async def index_schema_objects(project_id: str, include_db_tables: bool = False) -> str:
        """
        Extract and embed schema objects for a project, building the object
        embedding layer used by check_performance to score N+1 findings.

        Two object types are extracted:
        - Python classes from the call graph (all projects)
        - Postgres DB tables with FK relationships and cardinality (set
          include_db_tables=True for projects that use this Scopenos database)

        Each object is embedded as a structured description capturing what it
        represents, what it relates to, and its cardinality class (LOW / MEDIUM /
        HIGH / UNBOUNDED). check_performance uses these embeddings to distinguish
        high-cardinality correlated access patterns (likely real performance issues)
        from low-cardinality intentional loops (likely fine).

        Run this after index_project to enable object-embedding-enhanced scoring.
        """
        from ..schema_objects import index_schema_objects as _index
        svcs = await _tools_shared.get_services()
        await check_permission(get_current_user(), project_id, "write", svcs.db)
        result = await _index(
            svcs.db, svcs.embeddings, project_id,
            include_db_tables=include_db_tables,
        )
        return json.dumps(result)

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
