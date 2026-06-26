#!/usr/bin/env python3
"""
Scopenos database health check.

Connects to the configured Postgres instance, runs coverage and quality
queries across all tables, and writes a Markdown report.

Usage:
    python3 scripts/db_health.py                        # write to docs/db_health.md
    python3 scripts/db_health.py --output /tmp/out.md  # custom path
    python3 scripts/db_health.py --project django-13606 # focus on one project
    DATABASE_URL=postgres://... python3 scripts/db_health.py
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import asyncpg
except ImportError:
    print("asyncpg not installed. Run: pip install asyncpg", file=sys.stderr)
    sys.exit(1)

_DEFAULT_DSN = "postgresql://scopenos:scopenos@localhost:5432/scopenos"


# ── helpers ───────────────────────────────────────────────────────────────────

def _pct(n: int, total: int) -> str:
    if total == 0:
        return "n/a"
    return f"{100 * n / total:.1f}%"


def _bar(n: int, total: int, width: int = 20) -> str:
    if total == 0:
        return " " * width
    filled = round(width * n / total)
    return "█" * filled + "░" * (width - filled)


def _mask_dsn(dsn: str) -> str:
    """Hide password in DSN for display."""
    import re
    return re.sub(r":[^:@]+@", ":***@", dsn)


# ── queries ───────────────────────────────────────────────────────────────────

_PROJECT_SCOPED_TABLES = {
    "nodes":                    "project_id",
    "edges":                    "project_id",
    "function_embeddings":      "project_id",
    "schema_object_embeddings": "project_id",
    "decisions":                "project_id",
    "project_home_snapshots":   "project_id",
    "dependency_fingerprints":  "project_id",
    "module_patterns":          "project_id",
    "branch_function_changes":  "project_id",
    "agent_improvements":       "project_id",
    "contract_violations":      "project_id",
}


async def fetch_table_counts(conn, project_id: str | None = None) -> list[dict]:
    # Global counts from pg_stat (fast, approximate)
    global_rows = await conn.fetch("""
        SELECT relname AS table_name, n_live_tup AS row_count
        FROM pg_stat_user_tables
        ORDER BY n_live_tup DESC
    """)
    results = []
    for r in global_rows:
        tname = r["table_name"]
        count = r["row_count"]
        scoped = False
        if project_id and tname in _PROJECT_SCOPED_TABLES:
            col = _PROJECT_SCOPED_TABLES[tname]
            row = await conn.fetchrow(
                f"SELECT COUNT(*) AS cnt FROM {tname} WHERE {col} = $1",
                project_id,
            )
            count = row["cnt"]
            scoped = True
        results.append({"table_name": tname, "row_count": count, "scoped": scoped})
    return results


async def fetch_db_size(conn) -> str:
    row = await conn.fetchrow("SELECT pg_size_pretty(pg_database_size(current_database())) AS size")
    return row["size"]


async def fetch_projects(conn) -> list[dict]:
    rows = await conn.fetch("""
        SELECT
            p.id,
            p.name,
            p.branch,
            p.head_commit,
            p.last_indexed,
            COALESCE(nc.cnt, 0)  AS node_count,
            COALESCE(ec.cnt, 0)  AS edge_count,
            dp.project_id IS NOT NULL AS is_demo
        FROM projects p
        LEFT JOIN (SELECT project_id, COUNT(*) AS cnt FROM nodes GROUP BY project_id) nc
            ON nc.project_id = p.id
        LEFT JOIN (SELECT project_id, COUNT(*) AS cnt FROM edges GROUP BY project_id) ec
            ON ec.project_id = p.id
        LEFT JOIN demo_projects dp ON dp.project_id = p.id
        ORDER BY nc.cnt DESC NULLS LAST
    """)
    return [dict(r) for r in rows]


async def fetch_node_coverage(conn, project_id: str | None) -> list[dict]:
    pid_filter = "AND n.project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT
            n.project_id,
            COUNT(*)                                                         AS total_nodes,
            SUM(CASE WHEN n.is_external = 0 THEN 1 ELSE 0 END)             AS internal_nodes,
            SUM(CASE WHEN n.is_external = 1 THEN 1 ELSE 0 END)             AS external_nodes,
            SUM(CASE WHEN n.summary  != '' AND n.is_external = 0 THEN 1 ELSE 0 END) AS with_summary,
            SUM(CASE WHEN n.docstring != '' AND n.is_external = 0 THEN 1 ELSE 0 END) AS with_docstring,
            SUM(CASE WHEN n.body_hash != '' AND n.is_external = 0 THEN 1 ELSE 0 END) AS with_body_hash,
            SUM(CASE WHEN n.body      != '' AND n.is_external = 0 THEN 1 ELSE 0 END) AS with_body,
            COUNT(DISTINCT fe.id)                                            AS with_embedding
        FROM nodes n
        LEFT JOIN function_embeddings fe
            ON fe.id = n.id AND fe.project_id = n.project_id AND n.is_external = 0
        WHERE 1=1 {pid_filter}
        GROUP BY n.project_id
        ORDER BY total_nodes DESC
    """, *params)
    return [dict(r) for r in rows]


async def fetch_node_types(conn, project_id: str | None) -> list[dict]:
    pid_filter = "AND project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT project_id, type, COUNT(*) AS cnt
        FROM nodes WHERE is_external = 0 {pid_filter}
        GROUP BY project_id, type
        ORDER BY project_id, cnt DESC
    """, *params)
    return [dict(r) for r in rows]


async def fetch_top_subsystems(conn, project_id: str | None, limit: int = 12) -> list[dict]:
    pid_filter = "AND project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT
            project_id,
            CASE
                WHEN ARRAY_LENGTH(STRING_TO_ARRAY(module, '.'), 1) >= 2
                THEN ARRAY_TO_STRING(
                    (SELECT ARRAY(SELECT UNNEST(STRING_TO_ARRAY(module, '.')) LIMIT 2)),
                    '.'
                )
                ELSE module
            END AS subsystem,
            COUNT(*) AS fn_count
        FROM nodes
        WHERE is_external = 0 {pid_filter}
        GROUP BY project_id, subsystem
        ORDER BY project_id, fn_count DESC
        LIMIT {limit * (1 if project_id else 999)}
    """, *params)
    return [dict(r) for r in rows]


async def fetch_orphan_counts(conn, project_id: str | None) -> list[dict]:
    pid_filter = "AND n.project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT n.project_id, COUNT(*) AS orphan_count
        FROM nodes n
        WHERE n.is_external = 0
        {pid_filter}
        AND NOT EXISTS (
            SELECT 1 FROM edges e
            WHERE e.project_id = n.project_id AND e.caller_id = n.id
        )
        AND NOT EXISTS (
            SELECT 1 FROM edges e
            WHERE e.project_id = n.project_id AND e.callee_id = n.id
        )
        GROUP BY n.project_id
    """, *params)
    return [dict(r) for r in rows]


async def fetch_decision_counts(conn, project_id: str | None) -> list[dict]:
    pid_filter = "AND project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT project_id, COUNT(*) AS decision_count
        FROM decisions WHERE 1=1 {pid_filter}
        GROUP BY project_id
    """, *params)
    return [dict(r) for r in rows]


async def fetch_contract_summary(conn) -> dict:
    row = await conn.fetchrow("""
        SELECT
            COUNT(*) FILTER (WHERE status = 'active') AS active,
            COUNT(*) FILTER (WHERE status = 'draft')  AS draft,
            COUNT(*)                                   AS total
        FROM contracts
    """)
    return dict(row)


async def fetch_enrichment_backlog(conn, project_id: str | None) -> list[dict]:
    """Nodes with body text but no summary — prime enrichment targets."""
    pid_filter = "AND project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT project_id,
            COUNT(*) FILTER (WHERE body != '' AND summary = '') AS needs_summary,
            COUNT(*) FILTER (WHERE body != '' AND body_hash = '') AS needs_body_hash
        FROM nodes WHERE is_external = 0 {pid_filter}
        GROUP BY project_id
    """, *params)
    return [dict(r) for r in rows]


async def fetch_embedding_model_breakdown(conn, project_id: str | None) -> list[dict]:
    """How many nodes have each embedding model (or none)."""
    pid_filter = "AND n.project_id = $1" if project_id else ""
    params = [project_id] if project_id else []
    rows = await conn.fetch(f"""
        SELECT
            n.project_id,
            COALESCE(NULLIF(fe.project_id, NULL), 'none') AS has_embedding,
            n.embedding_model AS node_model,
            COUNT(*) AS cnt
        FROM nodes n
        LEFT JOIN function_embeddings fe ON fe.id = n.id AND fe.project_id = n.project_id
        WHERE n.is_external = 0 {pid_filter}
        GROUP BY n.project_id, has_embedding, n.embedding_model
        ORDER BY n.project_id, cnt DESC
    """, *params)
    return [dict(r) for r in rows]


# ── report builder ────────────────────────────────────────────────────────────

def _section(lines: list[str], title: str) -> None:
    lines.append(f"\n## {title}\n")


def _subsection(lines: list[str], title: str) -> None:
    lines.append(f"\n### {title}\n")


def build_report(
    *,
    dsn: str,
    table_counts: list[dict],
    db_size: str,
    projects: list[dict],
    node_coverage: list[dict],
    node_types: list[dict],
    top_subsystems: list[dict],
    orphans: list[dict],
    decisions: list[dict],
    contracts: dict,
    backlog: list[dict],
    emb_breakdown: list[dict],
    focus_project: str | None,
    elapsed_ms: int,
) -> str:
    lines: list[str] = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines.append("# Scopenos Database Health Report")
    lines.append(f"\n_Generated: {now} — {elapsed_ms}ms — DB: {_mask_dsn(dsn)} — Size: {db_size}_")
    if focus_project:
        lines.append(f"\n> Focused on project: `{focus_project}`")

    # ── Schema overview ──────────────────────────────────────────────────────
    _section(lines, "Schema Overview")
    if focus_project:
        lines.append(f"_Project-scoped tables filtered to `{focus_project}`. Global tables show totals._\n")
    lines.append("| Table | Rows | Scope |")
    lines.append("|---|---:|---|")
    for t in table_counts:
        scope = f"project: `{focus_project}`" if t["scoped"] else "global"
        lines.append(f"| `{t['table_name']}` | {t['row_count']:,} | {scope} |")

    # ── Project inventory ────────────────────────────────────────────────────
    _section(lines, "Project Inventory")
    lines.append("| ID | Name | Nodes | Edges | Last Indexed | Demo |")
    lines.append("|---|---|---:|---:|---|:---:|")
    for p in projects:
        demo = "✓" if p["is_demo"] else ""
        indexed = p["last_indexed"][:16] if p["last_indexed"] else "never"
        lines.append(
            f"| `{p['id']}` | {p['name']} | {p['node_count']:,} "
            f"| {p['edge_count']:,} | {indexed} | {demo} |"
        )

    # ── Per-project coverage ─────────────────────────────────────────────────
    _section(lines, "Per-Project Coverage")

    cov_by_pid = {r["project_id"]: r for r in node_coverage}
    types_by_pid: dict[str, dict] = {}
    for r in node_types:
        types_by_pid.setdefault(r["project_id"], {})[r["type"]] = r["cnt"]
    orphans_by_pid = {r["project_id"]: r["orphan_count"] for r in orphans}
    decisions_by_pid = {r["project_id"]: r["decision_count"] for r in decisions}
    backlog_by_pid = {r["project_id"]: r for r in backlog}

    for p in projects:
        pid = p["id"]
        cov = cov_by_pid.get(pid)
        if not cov:
            continue

        internal = int(cov["internal_nodes"] or 0)
        total = int(cov["total_nodes"] or 0)
        embs = int(cov["with_embedding"] or 0)
        summ = int(cov["with_summary"] or 0)
        docs = int(cov["with_docstring"] or 0)
        body_hash = int(cov["with_body_hash"] or 0)
        with_body = int(cov["with_body"] or 0)
        external = int(cov["external_nodes"] or 0)
        orphan_ct = int(orphans_by_pid.get(pid, 0))
        dec_ct = int(decisions_by_pid.get(pid, 0))
        bl = backlog_by_pid.get(pid, {})
        needs_summary = int(bl.get("needs_summary", 0) or 0)

        _subsection(lines, f"`{pid}` — {p['name']}")

        # Benchmark readiness badge
        emb_pct_val = (100 * embs / internal) if internal else 0
        summ_pct_val = (100 * summ / internal) if internal else 0
        if emb_pct_val >= 80 and summ_pct_val >= 50:
            readiness = "🟢 **Ready**"
        elif emb_pct_val >= 40:
            readiness = "🟡 **Partial** — embeddings present but coverage low"
        else:
            readiness = "🔴 **Not ready** — insufficient embedding coverage"
        lines.append(f"**Benchmark readiness:** {readiness}\n")

        lines.append("| Metric | Count | Coverage | Bar |")
        lines.append("|---|---:|---:|---|")

        def _row(label, n, denom=internal):
            return f"| {label} | {n:,} | {_pct(n, denom)} | `{_bar(n, denom)}` |"

        lines.append(f"| Total nodes (all) | {total:,} | — | |")
        lines.append(f"| Internal nodes | {internal:,} | — | |")
        lines.append(f"| External stubs | {external:,} | — | |")
        lines.append(_row("With embedding", embs))
        lines.append(_row("With LLM summary", summ))
        lines.append(_row("With docstring", docs))
        lines.append(_row("With body source", with_body))
        lines.append(_row("With body_hash", body_hash))
        lines.append(f"| Orphan nodes | {orphan_ct:,} | {_pct(orphan_ct, internal)} | `{_bar(orphan_ct, internal)}` |")
        lines.append(f"| Decisions logged | {dec_ct:,} | — | |")
        lines.append(f"| Edges | {p['edge_count']:,} | — | |")

        # Node types
        t = types_by_pid.get(pid, {})
        if t:
            lines.append(f"\n**Node types (internal):** " +
                         ", ".join(f"`{k}` {v:,}" for k, v in sorted(t.items(), key=lambda x: -x[1])))

        # Enrichment backlog
        if needs_summary > 0:
            lines.append(f"\n**Enrichment backlog:** {needs_summary:,} nodes have source body but no summary — "
                         f"estimated ${needs_summary * 0.0003:.2f} to enrich (@ $0.30/1k).")

        # Top subsystems
        sub_rows = [r for r in top_subsystems if r["project_id"] == pid][:12]
        if sub_rows:
            lines.append("\n**Top subsystems by function count:**\n")
            lines.append("| Subsystem | Functions |")
            lines.append("|---|---:|")
            for s in sub_rows:
                lines.append(f"| `{s['subsystem']}` | {s['fn_count']:,} |")

    # ── Contracts ────────────────────────────────────────────────────────────
    _section(lines, "Contracts")
    lines.append(f"- Active: {contracts.get('active', 0)}")
    lines.append(f"- Draft: {contracts.get('draft', 0)}")
    lines.append(f"- Total: {contracts.get('total', 0)}")

    # ── Benchmark readiness summary ──────────────────────────────────────────
    _section(lines, "Benchmark Readiness Summary")
    lines.append("| Project | Embedding % | Summary % | Verdict |")
    lines.append("|---|---:|---:|---|")
    for p in projects:
        pid = p["id"]
        cov = cov_by_pid.get(pid)
        if not cov:
            continue
        internal = int(cov["internal_nodes"] or 0)
        embs = int(cov["with_embedding"] or 0)
        summ = int(cov["with_summary"] or 0)
        ep = _pct(embs, internal)
        sp = _pct(summ, internal)
        ev = (100 * embs / internal) if internal else 0
        sv = (100 * summ / internal) if internal else 0
        if ev >= 80 and sv >= 50:
            verdict = "🟢 Ready"
        elif ev >= 40:
            verdict = "🟡 Partial"
        else:
            verdict = "🔴 Not ready"
        lines.append(f"| `{pid}` | {ep} | {sp} | {verdict} |")

    lines.append("\n_Embedding coverage drives `query_similar_functions`. "
                 "Summary coverage enriches result context. "
                 "Both must be high for Path B to outperform Path A._")

    # ── Tool reference ───────────────────────────────────────────────────────
    _section(lines, "Tool Reference")
    lines.append("_What each Scopenos MCP tool returns. All tools require `project_id`._\n")

    lines.append("### `get_project_home(project_id)`")
    lines.append("Architectural snapshot. Call this first every session.\n")
    lines.append("```json")
    lines.append("""{
  "subsystems": [
    {
      "name": "django.db",
      "function_count": 3817,
      "anchor": "django.db.models.Model",
      "anchor_summary": "Base class for all ORM model instances",
      "top_functions": [
        {"id": "django.db.models.Model.__eq__", "caller_count": 38}
      ]
    }
  ],
  "connections": [
    {"from": "django.db", "to": "django.db.models.sql", "edge_count": 84}
  ],
  "chokepoints": [
    {"id": "django.db.models.Model.save", "caller_count": 201}
  ],
  "recent_decisions": []
}""")
    lines.append("```\n")

    lines.append("### `query_similar_functions(snippet, project_id, top_k=10)`")
    lines.append("Semantic search — find functions by concept, not name. Use when you don't know the exact symbol.\n")
    lines.append("```json")
    lines.append("""{
  "results": [
    {
      "id": "django.db.models.Model.__eq__",
      "name": "__eq__",
      "summary": "Compare model instances by pk",
      "file": "django/db/models/base.py",
      "signature": "def __eq__(self, other)",
      "similarity": 0.94
    }
  ],
  "_guidance": {"next_step": "call get_impact_radius on the top result id"}
}""")
    lines.append("```\n")

    lines.append("### `get_impact_radius(function_name, project_id, depth=2)`")
    lines.append("BFS outward from a function — what breaks if this changes. "
                 "Also returns `co_change_hints` with three signals: protocol gaps, "
                 "semantic siblings not in the call graph, and git co-change history.\n")
    lines.append("```json")
    lines.append("""{
  "impact_radius": [
    {"id": "django.db.models.Model.__eq__", "impact_depth": 0,
     "file": "django/db/models/base.py", "signature": "def __eq__(self, other)"},
    {"id": "django.db.models.Model.pk",    "impact_depth": 1, "file": "..."}
  ],
  "co_change_hints": [
    {
      "type": "protocol_completeness",
      "message": "`__eq__` defined but `__hash__` not found on django.db.models.Model. Python requires both.",
      "suggested_id": "django.db.models.Model.__hash__",
      "action": "add"
    },
    {
      "type": "semantic_sibling",
      "message": "`__eq__` on AbstractUser is semantically similar but not reachable via call edges.",
      "id": "django.contrib.auth.models.AbstractUser.__eq__",
      "file": "django/contrib/auth/models.py",
      "similarity": 0.91
    },
    {
      "type": "co_change_history",
      "message": "`__lt__` has changed together with `__eq__` 7 times in git history — likely needs a parallel update.",
      "id": "django.db.models.Model.__lt__",
      "co_change_count": 7
    }
  ]
}""")
    lines.append("```\n")
    lines.append("**`co_change_hints` signal types:**\n")
    lines.append("| Type | Source | Fires when |")
    lines.append("|---|---|---|")
    lines.append("| `protocol_completeness` | Hardcoded dunder pairs | `__eq__` defined but `__hash__` missing on the same class |")
    lines.append("| `semantic_sibling` | Embedding similarity | Function is conceptually similar but unreachable via call edges |")
    lines.append("| `co_change_history` | Git commit history | Function appears in the same commits ≥3 times (`commit_function_changes` table) |")
    lines.append("\n_`co_change_history` is silent when `commit_function_changes` is empty — run `scripts/backfill_cochange.py` first._\n")

    lines.append("### `get_callers(function_name, project_id)`")
    lines.append("Every function that calls this one — with file and signature.\n")
    lines.append("```json")
    lines.append("""{
  "callers": [
    {
      "id": "django.test.TestCase.assertQuerysetEqual",
      "name": "assertQuerysetEqual",
      "file": "django/test/testcases.py",
      "signature": "def assertQuerysetEqual(self, qs, values, ...)"
    }
  ],
  "_guidance": {"next_step": "read the callers to understand usage contracts"}
}""")
    lines.append("```\n")

    lines.append("### `get_callees(function_name, project_id)`")
    lines.append("Every function this one calls — `is_external` flags stdlib/third-party symbols.\n")
    lines.append("```json")
    lines.append("""{
  "callees": [
    {"id": "django.db.models.sql.compiler.SQLCompiler.execute_sql",
     "name": "execute_sql", "is_external": false,
     "file": "django/db/models/sql/compiler.py"},
    {"id": "external.builtins.hash",
     "name": "hash", "is_external": true, "file": ""}
  ],
  "_guidance": {"next_step": "check is_external=false callees for cascading impact"}
}""")
    lines.append("```\n")

    lines.append("### `get_subsystem_detail(project_id, subsystem_name)`")
    lines.append("Full function list and wiring for one subsystem. "
                 "Call before reading any file in that subsystem — avoids reading large files blind.\n")
    lines.append("```json")
    lines.append("""{
  "subsystem": "tests.model_tests",
  "anchor_summary": "Base model test fixtures and assertion helpers",
  "top_functions": [
    {"id": "tests.model_tests.ModelTests.test_eq",
     "summary": "Tests Model.__eq__ with pk comparison",
     "caller_count": 0}
  ],
  "connections": [
    {"from": "tests.model_tests", "to": "django.db.models", "edge_count": 47}
  ]
}""")
    lines.append("```\n")

    lines.append("### `get_decision_history(function_name, project_id)`")
    lines.append("Every logged decision linked to this function — architectural, design, "
                 "implementation, and patch. Run before editing any function you didn't write.\n")
    lines.append("```json")
    lines.append("""{
  "decisions": [
    {
      "id": "abc-123",
      "type": "Implementation",
      "description": "Changed __eq__ to compare by pk only — see issue #13606",
      "rejected_alternatives": "Considered value equality but breaks ORM identity assumptions",
      "trigger": "git:bb45e94",
      "created_at": "2026-06-20T00:00:00"
    }
  ],
  "_guidance": {"next_step": "check rejected_alternatives before making changes"}
}""")
    lines.append("```\n")

    lines.append("### Field glossary\n")
    lines.append("| Field | Meaning |")
    lines.append("|---|---|")
    lines.append("| `id` | Fully-qualified dotted symbol: `module.Class.method` |")
    lines.append("| `impact_depth` | BFS distance from the target function (0 = the function itself) |")
    lines.append("| `is_external` | `true` = stdlib or third-party; not in the project's call graph |")
    lines.append("| `caller_count` | How many other functions call this one — proxy for change risk |")
    lines.append("| `similarity` | Cosine similarity 0–1 from the embedding search |")
    lines.append("| `co_change_hints` | Functions likely needing a parallel change — three signals: `protocol_completeness`, `semantic_sibling`, `co_change_history` |")
    lines.append("| `_guidance` | Suggested next tool call based on what was returned |")

    return "\n".join(lines) + "\n"


# ── main ──────────────────────────────────────────────────────────────────────

async def main(dsn: str, output: Path, focus_project: str | None) -> None:
    t0 = datetime.now()

    conn = await asyncpg.connect(dsn)
    try:
        table_counts    = await fetch_table_counts(conn, focus_project)
        db_size         = await fetch_db_size(conn)
        projects        = await fetch_projects(conn)
        node_coverage   = await fetch_node_coverage(conn, focus_project)
        node_types      = await fetch_node_types(conn, focus_project)
        top_subs        = await fetch_top_subsystems(conn, focus_project)
        orphans         = await fetch_orphan_counts(conn, focus_project)
        decisions       = await fetch_decision_counts(conn, focus_project)
        contracts       = await fetch_contract_summary(conn)
        backlog         = await fetch_enrichment_backlog(conn, focus_project)
        emb_breakdown   = await fetch_embedding_model_breakdown(conn, focus_project)
    finally:
        await conn.close()

    elapsed_ms = int((datetime.now() - t0).total_seconds() * 1000)

    if focus_project:
        projects = [p for p in projects if p["id"] == focus_project] or projects

    report = build_report(
        dsn=dsn,
        table_counts=table_counts,
        db_size=db_size,
        projects=projects,
        node_coverage=node_coverage,
        node_types=node_types,
        top_subsystems=top_subs,
        orphans=orphans,
        decisions=decisions,
        contracts=contracts,
        backlog=backlog,
        emb_breakdown=emb_breakdown,
        focus_project=focus_project,
        elapsed_ms=elapsed_ms,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(report)
    print(f"Report written to: {output}")
    print(f"Projects indexed: {len(projects)}")
    print(f"Elapsed: {elapsed_ms}ms")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scopenos database health check")
    p.add_argument("--output", type=Path, default=Path("docs/db_health.md"),
                   help="Output path for the Markdown report (default: docs/db_health.md)")
    p.add_argument("--project", default=None,
                   help="Focus on a single project_id (default: all projects)")
    p.add_argument("--dsn", default=None,
                   help="PostgreSQL DSN (default: $DATABASE_URL or localhost)")
    return p.parse_args()


if __name__ == "__main__":
    args = _parse_args()
    dsn = args.dsn or os.getenv("DATABASE_URL", "")
    if not dsn:
        print(
            "ERROR: DATABASE_URL is not set.\n"
            "Export it before running:\n"
            "  export DATABASE_URL=postgresql://scopenos:PASSWORD@HOST/scopenos\n"
            "Then re-run: .venv/bin/python scripts/db_health.py",
            file=sys.stderr,
        )
        sys.exit(1)
    asyncio.run(main(dsn=dsn, output=args.output, focus_project=args.project))
