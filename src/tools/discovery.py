"""Discovery tools: project listing, branch comparison, project home, client setup."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Callable

from fastmcp import FastMCP
from starlette.exceptions import HTTPException

from ..auth import get_current_user, check_permission
from ._shared import check_read_access
from . import _shared as _tools_shared


def register(mcp: FastMCP, _unused_get_services: Callable = None) -> None:
    import sys as _sys
    _mod = _sys.modules[__name__]

    @mcp.tool()
    async def list_projects() -> str:
        """
        List all indexed projects with their stats (node count, edge count,
        last indexed timestamp). Use this to discover available project_id values
        before calling scoped query tools.
        """
        svcs = await _tools_shared.get_services()
        user = get_current_user()
        if user is None:
            raise HTTPException(status_code=401, detail="Authentication required")
        accessible = await svcs.db.get_accessible_project_ids(user["id"])
        all_projects = await svcs.db.list_projects()
        result = [p for p in all_projects if p["id"] in accessible]
        return json.dumps(result)

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
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id_a, svcs.db)
        await check_read_access(project_id_b, svcs.db)
        # compare_projects spans two schemas; org-level DB (public fallback) sees both
        # until Task 12 migrates data into per-project schemas.
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
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        result = await pdb.get_branch_conflicts(project_id, function_ids, current_branch)
        return json.dumps(result)

    @mcp.tool()
    async def setup_scopenos_client(
        project_root: str,
        project_id: str = "",
        claude_home: str = "",
        install_git_hook: bool = True,
        server_url: str = "",
    ) -> str:
        """
        First-time setup for a new Scopenos-integrated project. Call this once after
        connecting to the MCP server. Execute the returned script with Bash to
        complete setup — no other manual steps required.

        Installs on the client machine:
        - Pre-edit hook (~/.claude/hooks/scopenos-suggest.py): fires before every Edit,
          warns when editing chokepoints or risk-surface functions, and nudges toward
          Scopenos tools on grep/Read.
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
                    Defaults to the SCOPENOS_URL env var on the server, then http://localhost:3004.

        Usage:
            result = setup_scopenos_client("/Users/name/myproject", server_url="http://100.71.88.106:3004")
            # Then run: exec(result["setup_script"])  or  Bash(result["run_command"])
        """
        from ..client_setup import generate_setup_script, _default_claude_home
        from ..indexer import _derive_project_id

        if not project_root:
            return json.dumps({"status": "error", "detail": "project_root is required"})

        pid = project_id or _derive_project_id(project_root)
        home = claude_home or _default_claude_home()
        server_url = server_url or os.getenv("SCOPENOS_URL", "http://localhost:3004")

        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
        post_commit_path = os.path.join(scripts_dir, "post-commit.sh")
        try:
            with open(post_commit_path) as f:
                post_commit = f.read().replace(
                    'SCOPENOS_URL="${SCOPENOS_URL:-http://localhost:3004}"',
                    f'SCOPENOS_URL="${{SCOPENOS_URL:-{server_url}}}"',
                )
        except FileNotFoundError:
            post_commit = f'#!/usr/bin/env bash\n# post-commit hook\nexport SCOPENOS_URL="{server_url}"\n'
            install_git_hook = False

        script = generate_setup_script(
            project_root=project_root,
            scopenos_url=server_url,
            project_id=pid,
            claude_home=home,
            install_git_hook=install_git_hook,
            post_commit_content=post_commit,
        )

        return json.dumps({
            "status": "ready",
            "project_id": pid,
            "scopenos_url": server_url,
            "run_command": "python3 << 'Scopenos_SETUP'\n" + script + "\nScopenos_SETUP",
            "setup_script": script,
            "instructions": (
                "Execute run_command with Bash to complete setup. "
                "Then restart Claude Code to activate hooks. "
                f"Then call index_project('{project_root}') to index the codebase."
            ),
        })

    @mcp.tool()
    async def get_project_home(project_id: str) -> str:
        """
        [DISCOVERY TOOL — accepts a project_id, returns full architectural picture]

        Architectural intelligence snapshot for a project. Call this FIRST at the
        start of any session before reading files or forming an implementation plan.

        Returns in a single call:
        - subsystems: top-30 module groups, each with:
            * function_count
            * anchor: the most-called class (representative of the subsystem)
            * anchor_summary: what the anchor does (80 chars)
            * top_functions: top-3 functions by caller count
        - connections: top-30 cross-subsystem wiring pairs (from, to, edge_count)
        - chokepoints: top-5 functions by caller count — touch carefully
        - recent_decisions: what changed in this codebase recently and why

        HOW TO USE:
        1. Scan anchor_summary for each subsystem — these describe the subsystem's
           architectural role in plain language. If anchor_summary is populated, you
           understand the design without reading any files.
        2. Scan top_functions to identify which subsystem contains the code you need.
        3. Call get_subsystem_detail(project_id, subsystem_name) for the full function
           list and the complete anchor_summary for that subsystem.
        4. Call query_similar_functions to find a specific function by description.
        5. Call get_callers / get_callees on that function BEFORE editing — they show
           how your return value will be consumed and what contracts to preserve.
        6. Read() only the exact lines you will change, after all of the above.
        """
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        from ..architecture_service import ArchitectureService as _AS
        # Reuse the ArchitectureService pinned to the cached pdb so its
        # TTL cache survives across requests (creating a new instance each
        # call would always miss — the cache is instance-level).
        if not hasattr(pdb, "_arch_service"):
            pdb._arch_service = _AS(pdb)
        result = await pdb._arch_service.get_project_home(project_id, max_age_seconds=300)
        return json.dumps(result)

    @mcp.tool()
    async def get_subsystem_detail(project_id: str, subsystem_name: str) -> str:
        """
        [DRILL-DOWN TOOL — call after get_project_home to focus on one subsystem]

        Full detail for a single subsystem identified in get_project_home:
        - anchor: the most-called class in this subsystem, with its full summary
          (the anchor_summary describes the architectural role of the subsystem —
          read this first, it explains how the subsystem works before you read code)
        - top 50 functions by caller count with summaries and signatures
        - ALL cross-subsystem connections (from/to with edge counts)

        The anchor_summary is the key output — it describes the subsystem's
        architecture in plain language so you understand the design before
        touching any file. If anchor_summary is empty, run enrich_summaries first.

        project_id: same project_id used in get_project_home
        subsystem_name: exact name from the subsystems list (e.g. "django.db.models.sql")

        Example response:
        {
          "subsystem": "django.db.models.sql",
          "function_count": 312,
          "anchor": "django.db.models.sql.query.Query",
          "anchor_summary": "Query is the internal SQL query builder. It compiles Q objects
            into a WhereNode tree via build_filter/_add_q. Exclusions (~Q) create a negated
            WhereNode — the child clause returned by split_exclude is wrapped in NOT(...).
            The compiler then renders WhereNode.as_sql() into the final SQL string.",
          "top_functions": [
            {"id": "django.db.models.sql.query.Query.build_filter", "caller_count": 12,
             "summary": "Converts a single filter expression into a WhereNode child."},
            ...
          ],
          "connections": [
            {"from": "django.db.models.sql", "to": "django.db.models.sql.where", "edge_count": 84}
          ]
        }
        """
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        from ..architecture_service import ArchitectureService as _AS
        from ..analysis import ArchitectureAnalyzer
        if not hasattr(pdb, "_arch_service"):
            pdb._arch_service = _AS(pdb)
        data = await pdb._arch_service.get_graph_data(project_id, max_age_seconds=300)
        result = ArchitectureAnalyzer().subsystem_detail(data, subsystem_name)
        return json.dumps(result)

    for _fn in [list_projects, compare_branches, get_branch_conflicts,
                setup_scopenos_client,
                get_project_home, get_subsystem_detail]:
        setattr(_mod, _fn.__name__, _fn)
