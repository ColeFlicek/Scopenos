"""Decision memory tools: log, query, and retrieve architectural decisions."""
from __future__ import annotations

import json
from typing import Callable

from fastmcp import FastMCP

from ..auth import get_current_user, check_permission
from ._shared import check_read_access
from . import _shared as _tools_shared


def register(mcp: FastMCP, _unused_get_services: Callable = None) -> None:
    import sys as _sys
    _mod = _sys.modules[__name__]

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
        svcs = await _tools_shared.get_services()
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
    async def get_decision_history(function_name: str, project_id: str) -> str:
        """
        [EXECUTION TOOL — accepts a symbol name]

        Return the full decision lineage for a function — every Architectural,
        Design, Implementation, and Patch decision linked to it, in chronological
        order. Call this before touching any function you did not write.

        project_id: required — scope results to this project. Pass "" to search all projects.
        """
        from ..guidance import compute_decision_guidance
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
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
        from ..decision_memory.memory import DecisionMemory as _DM
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        decisions = _DM(pdb, svcs.embeddings.with_db(pdb))
        results = await decisions.query_decisions(
            query_text, project_id=project_id or None
        )
        return json.dumps(results)

    for _fn in [log_decision, get_decision_history, query_decisions]:
        setattr(_mod, _fn.__name__, _fn)
