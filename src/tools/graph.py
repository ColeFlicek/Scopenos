"""Graph traversal tools: callers, callees, impact radius, function context, semantic search."""
from __future__ import annotations

import json
from typing import Callable

from fastmcp import FastMCP

from ._shared import check_read_access, fmt_contracts, contracts_for_name, ChangeHint
from . import _shared as _tools_shared


async def _co_change_hints(
    function_name: str,
    impact_results: list[dict],
    db,
    embeddings,
    project_id: str | None,
) -> list[ChangeHint]:
    """Derive co-change hints from protocol rules, semantic similarity, and pattern detection."""
    import asyncio as _asyncio

    target_nodes = [r for r in impact_results if r.get("impact_depth") == 0]
    if not target_nodes:
        target_nodes = await db.find_node_by_name(function_name, project_id)
    if not target_nodes:
        return []

    target = target_nodes[0]
    target_id = target["id"]
    target_name = target["name"]
    impact_ids = {r["id"] for r in impact_results}

    hints: list[ChangeHint] = []

    # ── Protocol completeness (Python dunder pairs) ───────────────────────────
    _PROTOCOL_PAIRS = {
        "__eq__": "__hash__",
        "__hash__": "__eq__",
        "__lt__": "__eq__",
        "__enter__": "__exit__",
        "__exit__": "__enter__",
    }
    paired_dunder = _PROTOCOL_PAIRS.get(target_name)
    if paired_dunder:
        try:
            siblings = await db.get_class_siblings(target_id, project_id)
        except Exception:
            siblings = []
        sibling_names = {s["name"] for s in siblings}
        if paired_dunder not in sibling_names:
            class_prefix = ".".join(target_id.split(".")[:-1])
            hints.append({
                "type": "protocol_completeness",
                "message": (
                    f"`{target_name}` defined but `{paired_dunder}` not found on "
                    f"`{class_prefix}`. Python requires both to be defined together."
                ),
                "suggested_id": f"{class_prefix}.{paired_dunder}",
                "action": "add",
            })
        else:
            paired_nodes = [s for s in siblings if s["name"] == paired_dunder]
            for pn in paired_nodes:
                if pn["id"] not in impact_ids:
                    hints.append({
                        "type": "protocol_completeness",
                        "message": (
                            f"`{paired_dunder}` exists on the same class and may "
                            f"need to be updated consistently with `{target_name}`."
                        ),
                        "suggested_id": pn["id"],
                        "action": "review",
                    })

    # ── Semantic siblings not in call graph ───────────────────────────────────
    target_signature = target.get("signature", "") or ""
    target_summary = target.get("summary") or target.get("docstring") or ""
    query_text = f"{target_name} {target_signature} {target_summary[:200]}"

    try:
        similar = await embeddings.query_similar(query_text.strip(), top_k=8, project_id=project_id)
    except Exception:
        similar = []
    for hit in similar:
        hid = hit.get("id", "")
        if hid and hid != target_id and hid not in impact_ids:
            hints.append({
                "type": "semantic_sibling",
                "message": (
                    f"`{hit.get('name', hid)}` is semantically similar to `{target_name}` "
                    f"but not reachable via call edges — may need a parallel change."
                ),
                "id": hid,
                "file": hit.get("file", ""),
                "similarity": hit.get("similarity", 0),
            })
            if len([h for h in hints if h["type"] == "semantic_sibling"]) >= 3:
                break

    # ── Visitor pattern obligations ───────────────────────────────────────────
    try:
        from ..pattern_detector import _detect_visitor
        target_body = target.get("body") or await db.get_node_body(target_id, project_id)
        target_enriched = {**target, "body": target_body}
        visitor_matches = await _detect_visitor(target_enriched, db, project_id)
        for vm in visitor_matches:
            if vm.role not in ("ConcreteVisitor",):
                continue
            sibling_visitors = vm.participants.get("SiblingVisitors", [])
            all_elements = vm.participants.get("Elements", [])
            target_class = ".".join(target_id.split(".")[:-1])
            if vm.missing:
                hints.append({
                    "type": "visitor_pattern",
                    "message": (
                        f"`{target_class}` implements the Visitor pattern "
                        f"but is missing handlers present in sibling visitors: "
                        + ", ".join(f"`{h}`" for h in vm.missing[:6])
                        + (f" (+{len(vm.missing)-6} more)" if len(vm.missing) > 6 else "")
                    ),
                    "visitor_classes": sibling_visitors,
                    "missing_handlers": vm.missing,
                    "action": vm.action or "add missing handlers to match sibling visitors",
                })
            else:
                hints.append({
                    "type": "visitor_pattern",
                    "message": (
                        f"`{target_class}` is one of {len(sibling_visitors) + 1} classes "
                        f"implementing the Visitor pattern ({len(all_elements)} element types covered). "
                        f"When adding a new element type, add handlers to all visitor classes."
                    ),
                    "visitor_classes": sibling_visitors,
                    "missing_handlers": [],
                    "action": "add handler to all visitor classes for any new element type",
                })
    except Exception:
        pass  # Visitor detection is best-effort; never block the main result

    # ── Git co-change history ─────────────────────────────────────────────────
    try:
        co_changed = await db.get_co_change_functions(
            target_id, project_id or "", min_count=3, limit=5
        )
    except Exception:
        co_changed = []
    for entry in co_changed:
        cid = entry.get("function_id", "")
        if cid and cid not in impact_ids and cid != target_id:
            hints.append({
                "type": "co_change_history",
                "message": (
                    f"`{cid.split('.')[-1]}` has changed together with `{target_name}` "
                    f"{entry['co_change_count']} times in git history — likely needs "
                    f"a parallel update."
                ),
                "id": cid,
                "co_change_count": entry["co_change_count"],
            })

    return hints


def register(mcp: FastMCP, _unused_get_services: Callable = None) -> None:
    import sys as _sys
    _mod = _sys.modules[__name__]

    @mcp.tool()
    async def get_callers(function_name: str, project_id: str = "") -> str:
        """
        [PRE-EDIT GATE — call this before editing any function]

        Returns every function that calls the specified function, with file, signature,
        and line range. Use this to understand how your return value or side effects will
        be consumed BEFORE you write any code. The callers' signatures reveal whether
        your output gets negated, wrapped, transformed, or used as-is — which determines
        whether your implementation is correct.

        Call get_callers before editing. Call get_callers on those callers if you still
        cannot predict the downstream effect of your change.

        Accepts a bare name, qualified name (module.func), or full id (module.Class.method).
        project_id: limit results to a specific project. If omitted, searches all projects.

        Example response:
        {
          "callers": [
            {
              "id": "django.db.models.sql.query.Query.build_filter",
              "name": "Query.build_filter",
              "file": "django/db/models/sql/query.py",
              "signature": "def build_filter(self, filter_expr, branch_negated=False, ...)",
              "start_line": 1488,
              "end_line": 1658
            }
          ],
          "_guidance": {
            "note": "1 caller(s) found for `split_exclude`",
            "signals": ["1/1 callers in `django.db.models.sql.query` — concentrated usage"],
            "suggested_follow_ups": []
          }
        }
        """
        from ..guidance import compute_callers_guidance
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        results = await pdb.get_callers(function_name, project_id or None)
        _contracts = await contracts_for_name(pdb, function_name, project_id)
        out: dict = {"callers": results, "_guidance": compute_callers_guidance(results, function_name)}
        if _contracts:
            out["applicable_contracts"] = fmt_contracts(_contracts)
        return json.dumps(out)

    @mcp.tool()
    async def get_callees(
        function_name: str, project_id: str = "", include_external: bool = False
    ) -> str:
        """
        [PRE-EDIT GATE — call this before editing any function]

        Returns every function called by the specified function, with file, signature,
        is_external flag, and line range. Use this to understand what contracts your
        rewrite must preserve — the callees reveal the dependencies your implementation
        relies on and must continue to satisfy.

        External callees (stdlib / third-party) are omitted by default — they are fixed
        contracts your rewrite must satisfy, not candidates for editing. Set
        include_external=True to include them (useful when auditing third-party dependency
        surface).

        project_id: limit results to a specific project. If omitted, searches all projects.

        Example response:
        {
          "callees": [
            {
              "id": "django.db.models.sql.where.WhereNode.add",
              "name": "WhereNode.add",
              "file": "django/db/models/sql/where.py",
              "signature": "def add(self, data, conn_type):",
              "is_external": 0,
              "start_line": 40,
              "end_line": 55
            }
          ],
          "_guidance": {
            "note": "3 callee(s) — 1 external (hidden, pass include_external=True to see)",
            "signals": ["1 external callee(s) — direct dependency on: builtins"],
            "suggested_follow_ups": []
          }
        }
        """
        from ..guidance import compute_callees_guidance
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        all_results = await pdb.get_callees(function_name, project_id or None)
        _contracts = await contracts_for_name(pdb, function_name, project_id)
        # Guidance runs on full results so external signal is preserved in the note.
        guidance = compute_callees_guidance(all_results, function_name)
        if not include_external:
            external_count = sum(1 for r in all_results if r.get("is_external"))
            results = [r for r in all_results if not r.get("is_external")]
            if external_count:
                guidance["note"] = (
                    f"{len(results)} internal callee(s) from `{function_name}` "
                    f"({external_count} external hidden — pass include_external=True to see)"
                )
        else:
            results = all_results
        out: dict = {"callees": results, "_guidance": guidance}
        if _contracts:
            out["applicable_contracts"] = fmt_contracts(_contracts)
        return json.dumps(out)

    @mcp.tool()
    async def get_impact_radius(
        function_name: str, depth: int = 2, project_id: str = "",
        compact: bool = False,
    ) -> str:
        """
        [EXECUTION TOOL — accepts a symbol name]

        BFS traversal outward from function_name up to `depth` levels.
        Returns every function that would be impacted by a change, plus
        co_change_hints — empirical signals about what else typically changes
        alongside this function in git history.

        co_change_hints has two types:
          - "co_change_history": functions that changed together with this one
            N times in past commits — strong signal they need parallel updates
          - "semantic_sibling": functions too similar to be coincidence but not
            reachable via call edges — may need the same fix applied

        READ co_change_hints carefully before editing. They surface hidden
        dependencies that call graph traversal alone cannot find.

        project_id: limit results to a specific project. If omitted, searches all projects.
        compact: True returns slim {name, module, impact_depth, id} nodes with a
        summary. Use when blast radius >20 nodes and you need scope not detail.

        Example response:
        {
          "impact_radius": [
            {"id": "django.db.models.sql.query.Query.split_exclude", "impact_depth": 0,
             "file": "django/db/models/sql/query.py", "signature": "def split_exclude(...)"},
            {"id": "django.db.models.sql.query.Query.build_filter", "impact_depth": 1, ...}
          ],
          "co_change_hints": [
            {"type": "co_change_history",
             "message": "`Lookup.__hash__` has changed together with `split_exclude` 5 times",
             "id": "django.db.models.lookups.Lookup.__hash__",
             "co_change_count": 5},
            {"type": "semantic_sibling",
             "message": "`WhereNode.split_having` is semantically similar but not reachable via call edges",
             "id": "django.db.models.sql.where.WhereNode.split_having",
             "similarity": 0.87}
          ]
        }
        """
        import asyncio as _asyncio
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pid = project_id or None
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        pemb = svcs.embeddings.with_db(pdb)

        results, _contracts = await _asyncio.gather(
            pdb.get_impact_radius(function_name, depth, pid),
            contracts_for_name(pdb, function_name, project_id),
        )

        hints = await _co_change_hints(function_name, results, pdb, pemb, pid)

        # Collect function IDs that hints explicitly reference — those depth-2+
        # entries are load-bearing and must be kept in full. Unreferenced depth-2+
        # entries are collapsed to a count: they add tokens but agents don't act on them.
        hint_ids: set[str] = set()
        for h in hints:
            if "id" in h:
                hint_ids.add(h["id"])
            if "suggested_id" in h:
                hint_ids.add(h["suggested_id"])

        kept, collapsed_count = [], 0
        for r in results:
            if r.get("impact_depth", 0) >= 2 and r["id"] not in hint_ids:
                collapsed_count += 1
            else:
                kept.append(r)
        if collapsed_count:
            results = kept

        if compact and results:
            by_module: dict[str, int] = {}
            by_depth: dict[int, int] = {}
            slim = []
            for node in results:
                mod = node.get("module", "")
                d = node.get("impact_depth", 0)
                by_module[mod] = by_module.get(mod, 0) + 1
                by_depth[str(d)] = by_depth.get(str(d), 0) + 1
                slim.append({"name": node["name"], "module": mod,
                              "impact_depth": d, "id": node["id"]})
            out: dict = {
                "impact_radius": slim,
                "impact_summary": {
                    "total": len(results),
                    "by_module": by_module,
                    "by_depth": by_depth,
                    "note": "compact=True — call with compact=False for full node objects (signatures, docstrings, source locations)",
                },
            }
        else:
            out = {"impact_radius": results}
            if collapsed_count:
                out["depth_2_trimmed"] = (
                    f"{collapsed_count} depth-2 node(s) omitted (not referenced by any "
                    "co_change hint). Pass compact=True or depth=1 for the full list."
                )
            elif len(results) > 20:
                out["_tip"] = (
                    f"{len(results)} nodes returned. Call with compact=True for "
                    "a module-level summary (total, by_module, by_depth) instead."
                )

        if hints:
            out["co_change_hints"] = hints
        if _contracts:
            out["applicable_contracts"] = fmt_contracts(_contracts)
        return json.dumps(out)

    @mcp.tool()
    async def query_similar_functions(
        snippet: str, project_id: str, top_k: int = 10
    ) -> str:
        """
        [DISCOVERY TOOL — accepts natural language or a code snippet]

        Hybrid BM25 + semantic search across the indexed codebase. Returns the
        top-k functions most relevant to your description, with file path, line
        range, signature, and summary for each.

        Use this to find a function when you don't know its name. Describe what
        the function does in plain English — e.g. "split exclude subquery for
        multi-valued relationships" or "NOT IN subquery generation".

        The returned `id` field is the stable function identifier to pass to
        get_callers, get_callees, get_impact_radius, and get_decision_history.

        project_id: required — scope to this project.
        top_k: number of results (default 10). Raise to 20 for broader sweep.

        Example response:
        {
          "results": [
            {
              "id": "django.db.models.sql.query.Query.split_exclude",
              "name": "Query.split_exclude",
              "file": "django/db/models/sql/query.py",
              "start_line": 1820,
              "end_line": 1887,
              "signature": "def split_exclude(self, filter_expr, can_reuse, names_with_path):",
              "summary": "Builds a NOT IN subquery for multi-valued relationship exclusions."
            },
            ...
          ],
          "_guidance": {
            "signals": ["Results concentrated in django.db.models.sql — check get_subsystem_detail"],
            "suggested_follow_ups": [
              {"tool": "get_callers", "args": {"function_name": "Query.split_exclude"},
               "reason": "Understand how return value is consumed before editing"}
            ]
          }
        }
        """
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        pemb = svcs.embeddings.with_db(pdb)
        results = await pemb.query_similar(snippet, top_k, project_id or None)
        # Top 3 results get full detail; lower-ranked results drop signature and
        # line ranges — agents rarely read past rank 3 for anything but the id/summary.
        _SLIM_KEYS = {"id", "name", "file", "summary", "similarity", "project_id"}
        for i in range(3, len(results)):
            results[i] = {k: v for k, v in results[i].items() if k in _SLIM_KEYS}
        if project_id and results:
            from ..guidance import compute_guidance
            guidance = await compute_guidance(results, pdb, project_id)
            return json.dumps({"results": results, "_guidance": guidance.to_dict()})
        return json.dumps(results)

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
        svcs = await _tools_shared.get_services()
        await check_read_access(project_id, svcs.db)
        pdb = await _tools_shared.resolve_project_db(project_id, svcs.db)
        result = await pdb.get_impact_radius(symbol, depth, project_id or None)
        return json.dumps(result)

    for _fn in [get_callers, get_callees, get_impact_radius,
                query_similar_functions, find_dependents]:
        setattr(_mod, _fn.__name__, _fn)
