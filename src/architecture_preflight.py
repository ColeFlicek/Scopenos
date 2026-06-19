"""
Pre-flight signal gathering for architectural review.

Aggregates four categories of structural signal from the call graph, performance
detector, and embeddings before the Explore agent walks the codebase — so it
knows where to look, not just that there's something to find.

Four signals
  1. Coupling hotspots   — high fan-in × fan-out functions (structural hubs)
  2. External scatter    — libraries used raw from many files (no adapter layer)
  3. Duplication seeds   — shared concepts spread across many files (missing deep module)
  4. Performance → cause — runtime findings converted to their structural root cause
"""
from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB
    from .performance import Finding


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class CouplingHotspot:
    function_id: str
    function_name: str
    file: str
    fan_in: int   # how many internal functions call this one
    fan_out: int  # how many functions this one calls

    @property
    def score(self) -> int:
        """Fan-in × fan-out hub score — high means structurally central."""
        return self.fan_in * self.fan_out


@dataclass
class ExternalScatter:
    library: str
    symbol_count: int       # distinct symbols from this library in use
    caller_file_count: int  # distinct internal files that import directly
    caller_count: int       # total call sites across all files

    @property
    def is_scattered(self) -> bool:
        """True when 3+ files import directly — adapter layer is probably missing."""
        return self.caller_file_count >= 3


@dataclass
class DuplicationCluster:
    concept: str         # seed phrase that surfaced this cluster
    matches: list[dict]  # [{name, file, module, similarity}]

    @property
    def file_spread(self) -> int:
        return len({m["file"] for m in self.matches})


_PATTERN_CAUSE: dict[str, str] = {
    "n_plus_one": (
        "Missing repository/batch layer — queries issued per item instead of in bulk"
    ),
    "external_call_in_loop": (
        "Missing adapter/concurrency abstraction — external latency serialized per iteration"
    ),
    "correlated_join_aggregate": (
        "Query logic leaking into wrong layer — aggregation SQL needs a dedicated query module"
    ),
    "sequential_awaits": (
        "Missing concurrency abstraction — independent I/O runs sequentially"
    ),
    "quadratic_expansion": (
        "Missing complexity bound at interface — O(n²) behaviour crosses a module boundary"
    ),
}


@dataclass
class PerformanceStructureSignal:
    pattern: str
    structural_cause: str
    affected_files: list[str]
    count: int


@dataclass
class ArchitecturePreflight:
    project_id: str
    coupling_hotspots: list[CouplingHotspot]
    external_scatter: list[ExternalScatter]
    duplication_clusters: list[DuplicationCluster]
    performance_signals: list[PerformanceStructureSignal]

    def to_brief(self) -> str:
        """Render as a focused Markdown brief for the architecture skill's Explore agent."""
        sections: list[str] = [
            f"## Architecture Pre-flight: `{self.project_id}`\n",
            "Focus exploration on these areas — each has quantified signal from the call graph.\n",
        ]

        # ── Coupling hotspots ──────────────────────────────────────────────────
        if self.coupling_hotspots:
            lines = ["### 1. Coupling Hotspots (fan-in × fan-out)\n",
                     "High score = structurally central. Apply the deletion test: "
                     "delete the module — does complexity vanish (pass-through) "
                     "or spread to all callers (load-bearing seam)?\n",
                     "| Function | Fan-in | Fan-out | Score | File |",
                     "|---|---|---|---|---|"]
            for h in self.coupling_hotspots:
                lines.append(
                    f"| `{h.function_name}` | {h.fan_in} | {h.fan_out} | "
                    f"**{h.score}** | {h.file} |"
                )
            sections.append("\n".join(lines))
        else:
            sections.append("### 1. Coupling Hotspots\n_No significant hubs detected._")

        # ── External scatter ───────────────────────────────────────────────────
        scattered = [s for s in self.external_scatter if s.is_scattered]
        contained = [s for s in self.external_scatter if not s.is_scattered]
        if self.external_scatter:
            lines = ["\n### 2. External Dependency Scatter\n",
                     "Libraries used directly from 3+ files likely lack an adapter layer. "
                     "One adapter = hypothetical seam. Two adapters (prod + test) = real seam.\n"]
            if scattered:
                lines.append("**Scattered (needs adapter):**")
                for s in scattered:
                    lines.append(
                        f"- **{s.library}** — {s.caller_file_count} files, "
                        f"{s.caller_count} call sites, {s.symbol_count} symbols"
                    )
            if contained:
                lines.append("\n**Contained (acceptable):**")
                for s in contained:
                    lines.append(
                        f"- {s.library} — {s.caller_file_count} file(s), "
                        f"{s.caller_count} call sites"
                    )
            sections.append("\n".join(lines))
        else:
            sections.append("\n### 2. External Dependency Scatter\n_No external dependencies indexed._")

        # ── Duplication clusters ───────────────────────────────────────────────
        spread_clusters = [c for c in self.duplication_clusters if c.file_spread >= 3]
        if spread_clusters:
            lines = ["\n### 3. Potential Duplication Clusters\n",
                     "Concepts found semantically similar in 3+ files — "
                     "may indicate a missing deep module that hasn't been named yet.\n"]
            for c in spread_clusters:
                fn_list = ", ".join(
                    f"`{m['name']}` ({m['file']})" for m in c.matches[:4]
                )
                lines.append(f"- **\"{c.concept}\"** (spread: {c.file_spread} files): {fn_list}")
            sections.append("\n".join(lines))
        else:
            sections.append("\n### 3. Duplication Clusters\n_No significant duplication detected._")

        # ── Performance → structure ────────────────────────────────────────────
        if self.performance_signals:
            lines = ["\n### 4. Performance Findings → Structural Causes\n",
                     "Runtime anti-patterns that point to missing abstraction layers. "
                     "Fix the structure, not just the symptom.\n"]
            for sig in self.performance_signals:
                files = ", ".join(sig.affected_files[:4])
                if len(sig.affected_files) > 4:
                    files += f" (+{len(sig.affected_files) - 4} more)"
                lines.append(
                    f"- **{sig.pattern}** ({sig.count} finding(s)) in: {files}\n"
                    f"  → {sig.structural_cause}"
                )
            sections.append("\n".join(lines))
        else:
            sections.append("\n### 4. Performance → Structure\n_No performance findings._")

        return "\n".join(sections)


# ── Gatherers ─────────────────────────────────────────────────────────────────

async def _gather_coupling_hotspots(
    db: "CallGraphDB",
    project_id: str,
    top_n: int = 8,
    min_score: int = 4,
) -> list[CouplingHotspot]:
    callee_map, all_nodes_list, internal_ids = await asyncio.gather(
        db.get_callee_map(project_id),
        db.get_all_nodes(project_id),
        db.get_internal_node_ids(project_id),
    )
    node_by_id = {n["id"]: n for n in all_nodes_list}

    fan_out: dict[str, int] = {}
    fan_in: dict[str, int] = defaultdict(int)
    for caller_id, callee_ids in callee_map.items():
        if caller_id not in internal_ids:
            continue
        fan_out[caller_id] = sum(1 for c in callee_ids if c in internal_ids)
        for callee_id in callee_ids:
            if callee_id in internal_ids:
                fan_in[callee_id] += 1

    all_ids = internal_ids & (set(fan_out) | set(fan_in))
    hotspots = []
    for fn_id in all_ids:
        fi = fan_in.get(fn_id, 0)
        fo = fan_out.get(fn_id, 0)
        score = fi * fo
        if score < min_score:
            continue
        node = node_by_id.get(fn_id, {})
        hotspots.append(CouplingHotspot(
            function_id=fn_id,
            function_name=node.get("name", fn_id.split(".")[-1]),
            file=node.get("file", ""),
            fan_in=fi,
            fan_out=fo,
        ))

    hotspots.sort(key=lambda h: -h.score)
    return hotspots[:top_n]


async def _gather_external_scatter(
    db: "CallGraphDB",
    project_id: str,
) -> list[ExternalScatter]:
    ext_deps, callee_map, all_nodes_list, internal_ids = await asyncio.gather(
        db.list_external_dependencies(project_id),
        db.get_callee_map(project_id),
        db.get_all_nodes(project_id),
        db.get_internal_node_ids(project_id),
    )
    node_by_id = {n["id"]: n for n in all_nodes_list}

    # Build: external_id -> set of caller files
    ext_caller_files: dict[str, set[str]] = defaultdict(set)
    for caller_id, callee_ids in callee_map.items():
        if caller_id not in internal_ids:
            continue
        caller_file = node_by_id.get(caller_id, {}).get("file", "")
        if not caller_file:
            continue
        for callee_id in callee_ids:
            if callee_id not in internal_ids:
                ext_caller_files[callee_id].add(caller_file)

    # Aggregate file counts per library using the already-grouped ext_deps
    result = []
    for lib_entry in ext_deps:
        lib = lib_entry["library"]
        total_callers = sum(s["caller_count"] for s in lib_entry["symbols"])
        caller_files: set[str] = set()
        for sym in lib_entry["symbols"]:
            caller_files.update(ext_caller_files.get(sym["id"], set()))
        result.append(ExternalScatter(
            library=lib,
            symbol_count=lib_entry["symbol_count"],
            caller_file_count=len(caller_files),
            caller_count=total_callers,
        ))

    return sorted(result, key=lambda s: -s.caller_file_count)


# Concepts that commonly get re-implemented across modules instead of
# being extracted into a single deep module.
_DUPLICATION_SEEDS = [
    "validate and normalize input parameters",
    "fetch record by id with error handling",
    "serialize response data to dict or JSON",
    "retry failed operation with exponential backoff",
    "paginate or batch query results",
    "parse and extract fields from raw data",
    "authenticate and authorize request",
    "format and emit structured log or error",
]


async def _gather_duplication_clusters(
    embeddings,
    project_id: str,
    top_k: int = 12,
    similarity_threshold: float = 0.72,
    min_file_spread: int = 3,
) -> list[DuplicationCluster]:
    store = getattr(embeddings, "_store", embeddings)

    async def _query(seed: str) -> tuple[str, list[dict]]:
        rows = await store.query_similar(seed, top_k=top_k, project_id=project_id)
        matches = [
            {"name": r["name"], "file": r["file"],
             "module": r["module"], "similarity": r["similarity"]}
            for r in rows
            if r.get("similarity", 0) >= similarity_threshold
        ]
        return seed, matches

    results = await asyncio.gather(*[_query(s) for s in _DUPLICATION_SEEDS])

    clusters = []
    for concept, matches in results:
        cluster = DuplicationCluster(concept=concept, matches=matches)
        if cluster.file_spread >= min_file_spread:
            clusters.append(cluster)

    return sorted(clusters, key=lambda c: -c.file_spread)


def _gather_performance_signals(
    findings: list["Finding"],
) -> list[PerformanceStructureSignal]:
    by_pattern: dict[str, list["Finding"]] = defaultdict(list)
    for f in findings:
        if not f.suppressed:
            by_pattern[f.pattern].append(f)

    signals = []
    for pattern, flist in by_pattern.items():
        cause = _PATTERN_CAUSE.get(pattern, f"Structural issue: {pattern}")
        files = sorted({f.file for f in flist if f.file})
        signals.append(PerformanceStructureSignal(
            pattern=pattern,
            structural_cause=cause,
            affected_files=files,
            count=len(flist),
        ))

    return sorted(signals, key=lambda s: -s.count)


# ── Entry point ───────────────────────────────────────────────────────────────

async def run_preflight(
    db: "CallGraphDB",
    project_id: str,
    performance_findings: list["Finding"] | None = None,
    embeddings=None,
) -> ArchitecturePreflight:
    """
    Gather all four architectural signals concurrently.

    performance_findings — pass the output of check_performance() to avoid
    running it twice. If None, performance signals are skipped.

    embeddings — the EmbeddingPipeline from _get_services(). If None,
    duplication cluster detection is skipped.
    """
    tasks: list = [
        _gather_coupling_hotspots(db, project_id),
        _gather_external_scatter(db, project_id),
    ]

    hotspots, scatter = await asyncio.gather(*tasks)

    clusters: list[DuplicationCluster] = []
    if embeddings is not None:
        clusters = await _gather_duplication_clusters(embeddings, project_id)

    perf_signals: list[PerformanceStructureSignal] = []
    if performance_findings is not None:
        perf_signals = _gather_performance_signals(performance_findings)

    return ArchitecturePreflight(
        project_id=project_id,
        coupling_hotspots=hotspots,
        external_scatter=scatter,
        duplication_clusters=clusters,
        performance_signals=perf_signals,
    )
