"""Quality tools: performance detection, SOLID checks, architecture preflight, code validation."""
from __future__ import annotations

import json
from typing import Callable

from fastmcp import FastMCP

from ..auth import get_current_user, check_permission
from ._shared import check_read_access


def register(mcp: FastMCP, get_services: Callable) -> None:

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
        from ..performance import check_performance as _check
        from ..guidance import compute_performance_guidance
        svcs = await get_services()
        await check_read_access(project_id, svcs.db)
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
        from ..validate import validate_proposed_code as _validate
        svcs = await get_services()
        await check_read_access(project_id, svcs.db)
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
        from ..performance import check_performance as _check_perf
        from ..architecture_preflight import run_preflight

        svcs = await get_services()
        await check_read_access(project_id, svcs.db)
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
        svcs = await get_services()
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
        from ..solid import check_solid as _check
        svcs = await get_services()
        await check_read_access(project_id, svcs.db)
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
        svcs = await get_services()
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
