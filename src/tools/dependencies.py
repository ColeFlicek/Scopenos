"""Dependency tools: fingerprinting, library dependents, health checks."""
from __future__ import annotations

import json
from typing import Callable

from fastmcp import FastMCP

from ..dependency_fingerprint import (
    DependencyFingerprinter,
    fingerprint_from_row,
    fingerprint_payload,
)
from ._shared import check_read_access
from . import _shared as _tools_shared


def register(mcp: FastMCP, _unused_get_services: Callable = None) -> None:
    import sys as _sys
    _mod = _sys.modules[__name__]

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
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        results = await pdb.list_external_dependencies(project_id)
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

        The fingerprint is also written to <project>/.scopenos/dependency-fingerprint.json
        on every index_project run, making dependency changes visible in git diff.
        """
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        row = await pdb.get_latest_dependency_fingerprint(project_id)
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
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        results = await pdb.list_dependency_fingerprint_history(project_id, limit)
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
        svcs = await _tools_shared.get_services()
        await check_read_access("", svcs.db)
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
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        results = await pdb.get_library_dependents(library_name, project_id)
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
        svcs = await _tools_shared.get_services()
        await check_read_access("", svcs.db)
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
        - symbols: all external symbols Scopenos has seen from this library
        - dependents: internal functions that call into this library, with call_count
        - recent_changes: version or symbol changes from the latest fingerprint diff

        This is the single-call answer to "is this dependency safe?" — combine with
        list_dependency_fingerprint_history to see the full change history.
        """
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        dependents = await pdb.get_library_dependents(library_name, project_id)
        fp_row = await pdb.get_latest_dependency_fingerprint(project_id)
        payload = fingerprint_payload(fp_row) if fp_row else None
        result = svcs.checker.health_envelope(library_name, dependents, payload)
        result["project_id"] = project_id
        return json.dumps(result)

    # Expose at module level for backwards-compat imports (e.g. test_mcp_tools.py)
    for _fn in [
        list_external_dependencies, get_dependency_fingerprint,
        list_dependency_fingerprint_history, get_dependency_fingerprint_at,
        get_library_dependents, compare_dependency_fingerprints, check_dependency,
    ]:
        setattr(_mod, _fn.__name__, _fn)
