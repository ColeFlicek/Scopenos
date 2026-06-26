"""
Guidance layer for Scopenos tool responses.

Classifies a set of query_similar_functions results into structural signals
and injects them as a _guidance field — so agents see patterns and constraints
without making additional discovery calls.

Seven signals (<20ms added latency, all computed from existing index data):
  1. concentration  — >75% of results in one module → surface module pattern
  2. chokepoint     — any result has >THRESHOLD callers → suggest get_impact_radius
  3. decision_gap   — high-caller result has no logged decisions → suggest get_decision_history
  4. contract       — active contract covers result module/function → inline constraint
  5. performance    — async results in storage/embeddings module → suggest check_performance
  6. async_dist     — uniform or mixed async/sync → surface convention
  7. naming         — dominant verb prefix in result names → surface naming pattern

Signals 1, 6, 7 are pure (0 DB calls). Signals 2–5 run as one asyncio.gather of
3 targeted DB queries. Backward compatible: callers that don't read _guidance are unaffected.
"""
from __future__ import annotations

import asyncio
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB

CHOKEPOINT_THRESHOLD: int = 15

# Modules where async functions are likely to have sequential-await or N+1 patterns.
_PERFORMANCE_SENSITIVE_MODULES: frozenset[str] = frozenset({
    "src.call_graph.storage",
    "src.embeddings.embedder",
    "src.embeddings.pipeline",
    "src.indexer",
    "src.decision_memory.memory",
})

_VERB_RE = re.compile(r"^_*([a-z]+)_")


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class FollowUp:
    tool: str
    args: dict
    reason: str


@dataclass
class Guidance:
    pattern_signal: str
    confidence: float
    active_constraints: list[str]
    signals: list[str]
    suggested_follow_ups: list[FollowUp]

    def to_dict(self) -> dict:
        return {
            "pattern_signal": self.pattern_signal,
            "confidence": self.confidence,
            "active_constraints": self.active_constraints,
            "signals": self.signals,
            "suggested_follow_ups": [
                {"tool": f.tool, "args": f.args, "reason": f.reason}
                for f in self.suggested_follow_ups
            ],
        }


# ── Pure signal detectors (0 DB calls) ───────────────────────────────────────

def _concentration_signal(results: list[dict]) -> tuple[str, float] | None:
    """Return (message, confidence) if results concentrate in one module, else None."""
    if not results:
        return None
    counts: Counter = Counter(r["module"] for r in results)
    dominant, cnt = counts.most_common(1)[0]
    ratio = cnt / len(results)
    if ratio >= 0.75:
        return (
            f"{cnt}/{len(results)} results in `{dominant}` — strong module concentration",
            ratio,
        )
    return None


def _async_signal(results: list[dict]) -> str | None:
    """Surface async/sync distribution. Returns None if all-sync (no signal).

    Uses the is_async field when present (set by precision parsers for all
    languages: Kotlin suspend fun, Swift async func, Python async def, etc.).
    Falls back to 'async def' text scan for embeddings results that don't
    include the field.
    """
    if not results:
        return None
    total = len(results)

    def _is_async(r: dict) -> bool:
        if "is_async" in r:
            return bool(r["is_async"])
        return "async def" in r.get("signature", "")

    async_count = sum(1 for r in results if _is_async(r))
    if async_count == 0:
        return None
    ratio = async_count / total
    if ratio >= 0.8:
        return f"Module is async-first ({async_count}/{total} results are async functions)"
    if ratio <= 0.2:
        return f"{async_count}/{total} async functions — module is mostly sync; async is the exception"
    return f"Mixed async/sync ({async_count}/{total} async) — verify which convention applies here"


def _naming_signal(results: list[dict]) -> str | None:
    """Return dominant verb prefix message if >60% of results share one, else None."""
    if not results:
        return None
    verbs: Counter = Counter()
    for r in results:
        name = r.get("name", "")
        if "." in name:
            name = name.split(".")[-1]
        m = _VERB_RE.match(name)
        if m:
            verbs[m.group(1)] += 1
    if not verbs:
        return None
    dominant_verb, cnt = verbs.most_common(1)[0]
    ratio = cnt / len(results)
    if ratio >= 0.6:
        return f"Dominant naming pattern: `{dominant_verb}_*` ({cnt}/{len(results)} functions)"
    return None


# ── DB-backed signal detectors ────────────────────────────────────────────────

def _chokepoint_follow_ups(
    caller_counts: dict[str, int],
    results: list[dict],
) -> list[FollowUp]:
    """Return get_impact_radius follow-ups for result functions above the chokepoint threshold."""
    id_to_name = {r["id"]: r.get("name", r["id"].split(".")[-1]) for r in results}
    chokepoints = [
        (fn_id, cnt)
        for fn_id, cnt in caller_counts.items()
        if cnt >= CHOKEPOINT_THRESHOLD and fn_id in id_to_name
    ]
    chokepoints.sort(key=lambda x: -x[1])
    return [
        FollowUp(
            tool="get_impact_radius",
            args={"function_name": id_to_name[fn_id], "depth": 2},
            reason=f"Chokepoint with {cnt} callers — changes propagate widely",
        )
        for fn_id, cnt in chokepoints
    ]


def _decision_gap_follow_ups(
    caller_counts: dict[str, int],
    functions_with_decisions: set[str],
    results: list[dict],
) -> list[FollowUp]:
    """Return get_decision_history follow-ups for high-caller functions with no decisions."""
    id_to_name = {r["id"]: r.get("name", r["id"].split(".")[-1]) for r in results}
    gaps = [
        (fn_id, cnt)
        for fn_id, cnt in caller_counts.items()
        if cnt >= CHOKEPOINT_THRESHOLD
        and fn_id not in functions_with_decisions
        and fn_id in id_to_name
    ]
    gaps.sort(key=lambda x: -x[1])
    return [
        FollowUp(
            tool="get_decision_history",
            args={"function_name": id_to_name[fn_id]},
            reason=f"High-caller function ({cnt} callers) with no logged decisions",
        )
        for fn_id, cnt in gaps
    ]


def _contract_constraints(
    contracts: list[dict],
    project_id: str,
    result_ids: list[str],
    result_modules: list[str],
) -> list[str]:
    """Return constraint messages for active contracts that cover any result function."""
    active = [c for c in contracts if c.get("status") == "active"]
    seen: set[str] = set()
    messages: list[str] = []
    for c in active:
        cid = c.get("id", "")
        if cid in seen:
            continue
        if project_id not in (c.get("project_ids") or []):
            continue
        fids = c.get("function_ids") or []
        if not fids:
            seen.add(cid)
            messages.append(f"Contract: {c['title']}")
            continue
        for fid in fids:
            matched = False
            if fid.endswith(".*"):
                prefix = fid[:-1]
                matched = any(rid.startswith(prefix) for rid in result_ids)
            else:
                matched = fid in result_ids
            if matched:
                seen.add(cid)
                messages.append(f"Contract: {c['title']}")
                break
    return messages


def _performance_suggestion(results: list[dict], project_id: str) -> FollowUp | None:
    """Suggest check_performance when async results appear in performance-sensitive modules."""
    has_async = any("async def" in r.get("signature", "") for r in results)
    if not has_async:
        return None
    modules = {r.get("module", "") for r in results}
    if not (modules & _PERFORMANCE_SENSITIVE_MODULES):
        return None
    return FollowUp(
        tool="check_performance",
        args={"project_id": project_id},
        reason="Async functions in I/O-heavy module — sequential await or N+1 pattern may be present",
    )


# ── Tool-specific guidance (pure, no DB) ─────────────────────────────────────

# Maps performance pattern keys → structural root cause description
PATTERN_CAUSE: dict[str, str] = {
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


def compute_callers_guidance(callers: list[dict], function_name: str) -> dict:
    """Pure guidance for get_callers responses.

    Surfaces caller concentration, chokepoint status, and async context.
    """
    if not callers:
        return {
            "note": (
                f"`{function_name}` has no callers — it may be an entry point, "
                "unused code, or only reachable via dynamic dispatch."
            ),
            "signals": [],
            "suggested_follow_ups": [],
        }

    signals: list[str] = []
    follow_ups: list[dict] = []

    # Concentration
    counts: Counter = Counter(c.get("module", "") for c in callers)
    dominant, cnt = counts.most_common(1)[0]
    ratio = cnt / len(callers)
    if ratio >= 0.75:
        signals.append(f"{cnt}/{len(callers)} callers in `{dominant}` — concentrated usage")
    elif len(counts) >= 5:
        signals.append(f"Callers span {len(counts)} modules — widely used function")

    # Chokepoint
    if len(callers) >= CHOKEPOINT_THRESHOLD:
        signals.append(
            f"{len(callers)} callers — this is a chokepoint; changes propagate to all callers"
        )
        follow_ups.append({
            "tool": "get_impact_radius",
            "args": {"function_name": function_name, "depth": 2},
            "reason": f"Chokepoint with {len(callers)} callers — understand full propagation before editing",
        })

    # Async context
    async_count = sum(1 for c in callers if "async def" in c.get("signature", ""))
    if async_count > 0 and async_count == len(callers):
        signals.append("All callers are async — this function runs exclusively in async context")
    elif async_count > 0 and async_count / len(callers) < 0.3:
        signals.append(
            f"{async_count}/{len(callers)} callers are async — mostly sync calling context"
        )

    note = f"{len(callers)} caller(s) found for `{function_name}`"
    return {"note": note, "signals": signals, "suggested_follow_ups": follow_ups}


def compute_callees_guidance(callees: list[dict], function_name: str) -> dict:
    """Pure guidance for get_callees responses.

    Surfaces external dependency exposure and callee concentration.
    """
    if not callees:
        return {
            "note": (
                f"`{function_name}` calls nothing — leaf function or all dependencies "
                "are injected/dynamic (not statically visible in the call graph)."
            ),
            "signals": [],
            "suggested_follow_ups": [],
        }

    signals: list[str] = []
    follow_ups: list[dict] = []

    # External dependency surface
    externals = [c for c in callees if c.get("is_external")]
    if externals:
        libs = sorted({c.get("module", "unknown") for c in externals})
        signals.append(
            f"{len(externals)} external callee(s) — direct dependency on: {', '.join(libs[:4])}"
        )
        if len(externals) >= 3:
            signals.append(
                "High external surface — consider whether an adapter layer would improve testability"
            )

    # Callee concentration
    internal = [c for c in callees if not c.get("is_external")]
    if internal:
        counts: Counter = Counter(c.get("module", "") for c in internal)
        dominant, cnt = counts.most_common(1)[0]
        ratio = cnt / len(internal)
        if ratio >= 0.75 and len(internal) > 2:
            signals.append(
                f"{cnt}/{len(internal)} internal callees in `{dominant}` — strong module coupling"
            )
            follow_ups.append({
                "tool": "get_decision_history",
                "args": {"function_name": dominant.split(".")[-1]},
                "reason": f"Strong coupling to `{dominant}` — check design decisions before modifying",
            })

    note = f"{len(callees)} callee(s) from `{function_name}` ({len(externals)} external)"
    return {"note": note, "signals": signals, "suggested_follow_ups": follow_ups}


def compute_decision_guidance(
    decisions: list[dict], function_name: str, project_id: str
) -> dict:
    """Pure guidance for get_decision_history responses.

    When empty: explains why decisions matter and suggests next steps.
    When non-empty: summarises the decision lineage.
    """
    if not decisions:
        return {
            "note": (
                f"No decisions logged for `{function_name}`. "
                "Logging decisions documents architectural intent for future agents — "
                "especially important for chokepoints or functions with non-obvious design."
            ),
            "signals": [],
            "suggested_follow_ups": [
                {
                    "tool": "get_callers",
                    "args": {"function_name": function_name, "project_id": project_id},
                    "reason": "Understand scope — high caller count means logging is especially valuable",
                },
                {
                    "tool": "log_decision",
                    "args": {
                        "type": "Design",
                        "description": f"Why {function_name} is designed this way...",
                        "project_id": project_id,
                    },
                    "reason": "Record the design intent so future agents don't have to guess",
                },
            ],
        }

    types = Counter(d.get("type", "Unknown") for d in decisions)
    type_summary = ", ".join(f"{v}× {k}" for k, v in types.most_common())
    most_recent = decisions[-1]
    return {
        "note": (
            f"{len(decisions)} decision(s) logged for `{function_name}`: {type_summary}. "
            f"Most recent: {most_recent.get('type')} — "
            f"{most_recent.get('description', '')[:80]}…"
        ),
        "signals": [],
        "suggested_follow_ups": [],
    }


def compute_performance_guidance(findings: list) -> dict:
    """Pure guidance for check_performance responses.

    Maps each pattern to its structural root cause and suggests fix direction.
    """
    if not findings:
        return {
            "note": "No performance concerns detected in indexed functions.",
            "structural_causes": [],
            "suggested_follow_ups": [],
        }

    active = [f for f in findings if not getattr(f, "suppressed", False)]
    if not active:
        return {
            "note": "All findings are acknowledged — no new concerns.",
            "structural_causes": [],
            "suggested_follow_ups": [],
        }

    by_pattern: dict[str, list] = {}
    for f in active:
        by_pattern.setdefault(f.pattern, []).append(f)

    structural_causes = []
    follow_ups = []
    for pattern, flist in sorted(by_pattern.items(), key=lambda x: -len(x[1])):
        cause = PATTERN_CAUSE.get(pattern, f"Structural issue: {pattern}")
        files = sorted({f.file for f in flist if f.file})
        structural_causes.append({
            "pattern": pattern,
            "count": len(flist),
            "structural_cause": cause,
            "affected_files": files[:4],
        })
        # Suggest impact_radius for the first affected function
        first = flist[0]
        follow_ups.append({
            "tool": "get_impact_radius",
            "args": {"function_name": first.function_name, "depth": 2},
            "reason": (
                f"`{first.function_name}` has a `{pattern}` finding — "
                "understand propagation before refactoring"
            ),
        })

    return {
        "note": f"{len(active)} active finding(s) across {len(by_pattern)} pattern(s).",
        "structural_causes": structural_causes,
        "suggested_follow_ups": follow_ups[:3],  # cap to avoid overwhelming
    }


# ── Entry point ───────────────────────────────────────────────────────────────

async def compute_guidance(
    results: list[dict],
    db: "CallGraphDB",
    project_id: str,
) -> Guidance:
    """
    Classify a query_similar_functions result set into structural signals.

    Runs 3 DB queries in parallel (caller counts, decision coverage, contracts),
    combines them with pure signals, and returns a Guidance object.

    Gracefully returns empty Guidance on empty results.
    """
    if not results:
        return Guidance(
            pattern_signal="",
            confidence=0.0,
            active_constraints=[],
            signals=[],
            suggested_follow_ups=[],
        )

    result_ids = [r["id"] for r in results]
    result_modules = [r.get("module", "") for r in results]

    # ── Pure signals (free) ────────────────────────────────────────────────────
    concentration = _concentration_signal(results)
    async_msg = _async_signal(results)
    naming_msg = _naming_signal(results)

    # ── DB-backed signals (one gather) ────────────────────────────────────────
    caller_counts, functions_with_decisions, contracts = await asyncio.gather(
        db.get_caller_counts(project_id, result_ids),
        db.get_functions_with_decisions(result_ids),
        db.list_contracts(project_id),
    )

    # ── Assemble ───────────────────────────────────────────────────────────────
    pattern_signal, confidence = concentration or ("", 0.0)

    secondary: list[str] = []
    if async_msg:
        secondary.append(async_msg)
    if naming_msg:
        secondary.append(naming_msg)

    constraints = _contract_constraints(contracts, project_id, result_ids, result_modules)

    follow_ups: list[FollowUp] = []
    follow_ups.extend(_chokepoint_follow_ups(caller_counts, results))
    follow_ups.extend(_decision_gap_follow_ups(caller_counts, functions_with_decisions, results))
    perf = _performance_suggestion(results, project_id)
    if perf:
        follow_ups.append(perf)

    return Guidance(
        pattern_signal=pattern_signal,
        confidence=confidence,
        active_constraints=constraints,
        signals=secondary,
        suggested_follow_ups=follow_ups,
    )
