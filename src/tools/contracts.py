"""Contract tools: create, approve, list, and check invariant contracts."""
from __future__ import annotations

import json
from typing import Callable

from fastmcp import FastMCP
from starlette.exceptions import HTTPException

from ..auth import get_current_user, check_permission
from ._shared import check_read_access


def register(mcp: FastMCP, get_services: Callable) -> None:

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
        svcs = await get_services()
        for _pid in (project_ids or []):
            await check_permission(get_current_user(), _pid, "write", svcs.db)
        result = await svcs.contracts.generate_draft(
            project_ids=project_ids or [],
            title=title,
            natural_language=natural_language,
            function_ids=function_ids,
        )
        return json.dumps(result)

    @mcp.tool()
    async def approve_contract(contract_id: str) -> str:
        """
        Activate a draft contract.

        Embeds all violation and compliance examples into vec0 tables so that
        semantic checking can run. Once active, the contract is enforced on
        every call to check_contracts() and via the post-commit hook.
        """
        svcs = await get_services()
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
        svcs = await get_services()
        await check_read_access(project_id, svcs.db)
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
        svcs = await get_services()
        await check_read_access(project_id, svcs.db)
        result = await svcs.contracts.check_project(project_id, semantic=semantic)
        return json.dumps(result)
