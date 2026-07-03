"""
Index health tool — check_index_health.

Returns a structured report on a project's index completeness:
  - function count and breakdown
  - summary / embedding coverage with model breakdown
  - call edge coverage (orphan detection)
  - co_change history availability
  - anchor summary coverage per subsystem
  - actionable recommendations with priority
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register(mcp: "FastMCP", _unused: Callable = None) -> None:
    import sys as _sys
    _mod = _sys.modules[__name__]

    @mcp.tool()
    async def check_index_health(project_id: str) -> str:
        """
        [DIAGNOSTIC TOOL — returns index completeness report for a project]

        Checks every dimension of index quality and returns a prioritised action list.
        Run this before enrichment, benchmarking, or debugging poor search quality.

        Returns:
          overall_health: "healthy" | "partial" | "degraded" | "empty"
          coverage: per-dimension stats (functions, summaries, embeddings, edges,
                    co_change history, anchor summaries)
          actions: prioritised list of what to do next, with the exact tool/command
          last_indexed: ISO timestamp of most recent index update
          is_fork: whether this is a fork and who its parent is

        Example response:
        {
          "project_id": "django",
          "overall_health": "partial",
          "is_fork": false,
          "last_indexed": "2026-07-03T03:28:39+00:00",
          "coverage": {
            "functions": {"total": 10973, "internal": 9841, "external": 1132},
            "summaries": {"have": 7200, "missing": 2773, "pct": 65,
                          "note": "missing summaries degrade semantic search quality"},
            "embeddings": {
              "total": 10973,
              "by_model": {"text-embedding-3-small": 8000, "text-embedding-3-large": 2973},
              "large_model_fallback_pct": 27,
              "note": "large-model fallbacks are slower at query time and less precise"
            },
            "call_edges": {"total": 66554, "nodes_with_edges": 9500,
                           "orphan_nodes": 341, "orphan_pct": 3},
            "co_change_history": {"rows": 170368, "commits": 1183,
                                  "note": "populated — co_change_hints will fire"},
            "anchor_summaries": {"have": 12, "missing": 18, "pct": 40,
                                 "empty_anchors": ["django.db.models", "django.db.backends"]}
          },
          "actions": [
            {"priority": "high", "action": "enrich_summaries",
             "command": "POST /api/enrich-summaries/django {sync: true, limit: 500}",
             "reason": "2773 functions missing LLM summaries — run 6 batches of 500"},
            {"priority": "medium", "action": "push_cochange_history",
             "command": "python3 scripts/push_cochange_history.py --project-id django --limit 2000",
             "reason": "co_change history present but may be stale after recent indexing"},
            {"priority": "low", "action": "re-index",
             "command": "index_changes or index_project",
             "reason": "last indexed 3 days ago — run after code changes"}
          ]
        }
        """
        from ..tools._shared import get_services, resolve_project_db
        from ..architecture_service import ArchitectureService

        svcs = await get_services()
        pdb = await resolve_project_db(project_id, svcs.db)
        schema = pdb._schema

        async with pdb.acquire() as conn:
            # ── 1. Function counts ─────────────────────────────────────────────
            func_row = await conn.fetchrow(
                f"""SELECT
                      COUNT(*) FILTER (WHERE is_external = 0) AS internal,
                      COUNT(*) FILTER (WHERE is_external != 0) AS external,
                      COUNT(*) AS total
                    FROM "{schema}".nodes
                    WHERE project_id = $1""",
                project_id,
            )
            total = int(func_row["total"])
            internal = int(func_row["internal"])
            external = int(func_row["external"])

            if total == 0:
                return json.dumps({
                    "project_id": project_id,
                    "overall_health": "empty",
                    "coverage": {},
                    "actions": [{"priority": "critical", "action": "index_project",
                                 "reason": "No functions indexed — run index_project first"}],
                })

            # ── 2. Summary coverage ────────────────────────────────────────────
            summary_row = await conn.fetchrow(
                f"""SELECT
                      COUNT(*) FILTER (WHERE summary != '' AND summary IS NOT NULL) AS have,
                      COUNT(*) FILTER (WHERE summary = ''  OR  summary IS NULL)     AS missing
                    FROM "{schema}".nodes
                    WHERE project_id = $1 AND is_external = 0""",
                project_id,
            )
            sum_have = int(summary_row["have"])
            sum_missing = int(summary_row["missing"])
            sum_pct = round(sum_have / internal * 100) if internal else 0

            # ── 3. Embedding model breakdown ───────────────────────────────────
            emb_rows = await conn.fetch(
                f"""SELECT embedding_model, COUNT(*) AS cnt
                    FROM "{schema}".nodes
                    WHERE project_id = $1 AND is_external = 0
                    GROUP BY embedding_model""",
                project_id,
            )
            by_model = {r["embedding_model"] or "none": int(r["cnt"]) for r in emb_rows}
            large_fallback = by_model.get("text-embedding-3-large", 0)
            large_pct = round(large_fallback / internal * 100) if internal else 0

            # ── 4. Call edge coverage ──────────────────────────────────────────
            edge_row = await conn.fetchrow(
                f"""SELECT
                      COUNT(*) AS total_edges,
                      COUNT(DISTINCT caller_id) AS nodes_with_out_edges
                    FROM "{schema}".edges
                    WHERE project_id = $1""",
                project_id,
            )
            total_edges = int(edge_row["total_edges"])
            nodes_with_edges = int(edge_row["nodes_with_out_edges"])
            orphan_nodes = internal - nodes_with_edges
            orphan_pct = round(orphan_nodes / internal * 100) if internal else 0

            # ── 5. co_change history ───────────────────────────────────────────
            cochange_row = await conn.fetchrow(
                f"""SELECT
                      COUNT(*) AS rows,
                      COUNT(DISTINCT commit_hash) AS commits
                    FROM "{schema}".commit_function_changes
                    WHERE project_id = $1""",
                project_id,
            )
            cochange_rows = int(cochange_row["rows"])
            cochange_commits = int(cochange_row["commits"])

            # ── 6. Project metadata (is_fork, last_indexed) ───────────────────
            meta_row = await conn.fetchrow(
                """SELECT is_fork, parent_schema, last_indexed, head_commit
                   FROM projects WHERE id = $1""",
                project_id,
            )

        is_fork = bool(meta_row["is_fork"]) if meta_row else False
        parent_schema = meta_row["parent_schema"] if meta_row else None
        _li = meta_row["last_indexed"] if meta_row else None
        last_indexed = _li.isoformat() if hasattr(_li, "isoformat") else (_li and str(_li))

        # ── 7. Anchor summary coverage (via architecture service) ──────────────
        if not hasattr(pdb, "_arch_service"):
            pdb._arch_service = ArchitectureService(pdb)
        arch_data = await pdb._arch_service.get_graph_data(project_id, max_age_seconds=300)

        from ..analysis import ArchitectureAnalyzer
        home = ArchitectureAnalyzer().project_home(arch_data, project_id)
        subsystems = home.get("subsystems", [])
        anchor_have = sum(1 for s in subsystems if s.get("anchor_summary", "").strip())
        anchor_missing = sum(1 for s in subsystems if not s.get("anchor_summary", "").strip())
        anchor_pct = round(anchor_have / len(subsystems) * 100) if subsystems else 0
        empty_anchors = [s["name"] for s in subsystems if not s.get("anchor_summary", "").strip()][:10]

        # ── 8. Build actions ───────────────────────────────────────────────────
        actions = []

        if total == 0:
            actions.append({"priority": "critical", "action": "index_project",
                            "reason": "No functions indexed"})
        else:
            if sum_missing > 0:
                batches = -(-sum_missing // 500)  # ceil division
                actions.append({
                    "priority": "high",
                    "action": "enrich_summaries",
                    "command": f"POST /api/enrich-summaries/{project_id} {{\"sync\": true, \"limit\": 500}}",
                    "reason": f"{sum_missing} internal functions missing LLM summaries — run {batches} batch(es) of 500",
                })

            if cochange_rows == 0:
                actions.append({
                    "priority": "high",
                    "action": "push_cochange_history",
                    "command": f"python3 scripts/push_cochange_history.py --project-id {project_id} --limit 2000",
                    "reason": "No co_change history — get_impact_radius co_change_hints will be empty",
                })
            elif cochange_rows < internal * 2:
                actions.append({
                    "priority": "medium",
                    "action": "push_cochange_history",
                    "command": f"python3 scripts/push_cochange_history.py --project-id {project_id} --limit 2000",
                    "reason": f"co_change history sparse ({cochange_rows} rows for {internal} functions) — more history improves hints",
                })

            if large_pct > 20:
                actions.append({
                    "priority": "medium",
                    "action": "enrich_summaries",
                    "command": f"POST /api/enrich-summaries/{project_id} {{\"sync\": true, \"limit\": 500}}",
                    "reason": f"{large_pct}% of functions used large-model embedding fallback — enrich to get precise small-model embeddings",
                })

            if orphan_pct > 30:
                actions.append({
                    "priority": "medium",
                    "action": "re-index",
                    "command": "index_project or index_changes",
                    "reason": f"{orphan_pct}% of functions have no outgoing call edges — index may be incomplete",
                })

        # ── 9. Overall health score ────────────────────────────────────────────
        high_actions = sum(1 for a in actions if a["priority"] == "high")
        if total == 0:
            health = "empty"
        elif high_actions >= 2:
            health = "degraded"
        elif high_actions == 1 or sum_pct < 50:
            health = "partial"
        else:
            health = "healthy"

        result = {
            "project_id": project_id,
            "overall_health": health,
            "is_fork": is_fork,
            **({"parent_schema": parent_schema} if is_fork else {}),
            "last_indexed": last_indexed,
            "coverage": {
                "functions": {"total": total, "internal": internal, "external": external},
                "summaries": {
                    "have": sum_have, "missing": sum_missing, "pct": sum_pct,
                    **({"note": "missing summaries degrade semantic search quality"} if sum_missing > 0 else {}),
                },
                "embeddings": {
                    "total_with_embeddings": internal,
                    "by_model": by_model,
                    "large_model_fallback": large_fallback,
                    "large_model_fallback_pct": large_pct,
                    **({"note": f"{large_pct}% used large-model fallback — enrich_summaries will re-embed with small model"} if large_pct > 10 else {}),
                },
                "call_edges": {
                    "total": total_edges,
                    "nodes_with_out_edges": nodes_with_edges,
                    "orphan_nodes": orphan_nodes,
                    "orphan_pct": orphan_pct,
                },
                "co_change_history": {
                    "rows": cochange_rows,
                    "commits": cochange_commits,
                    "note": (
                        "populated — co_change_hints will fire on get_impact_radius"
                        if cochange_rows > 0 else
                        "empty — run push_cochange_history to enable co_change_hints"
                    ),
                },
                "anchor_summaries": {
                    "subsystems_total": len(subsystems),
                    "have": anchor_have,
                    "missing": anchor_missing,
                    "pct": anchor_pct,
                    **({"empty_anchors": empty_anchors} if empty_anchors else {}),
                    **({"note": "run enrich_summaries to populate anchor summaries for get_project_home and get_subsystem_detail"} if anchor_missing > 0 else {}),
                },
            },
            "actions": actions,
        }

        return json.dumps(result)

    setattr(_mod, "check_index_health", check_index_health)
