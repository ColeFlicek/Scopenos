"""
validate_proposed_code: pre-flight conformance check for new code.

Parses proposed code in-memory with TreeSitterParser, compares it against the
indexed module's conventions, checks active contracts, and runs pure-body
performance detectors. Returns a conformance score (0–1) and specific
deviations with concrete examples from the existing module.

Four checks (all pure — no embeddings, no extra DB queries beyond what the
normal tool path already fetches):
  1. naming       — proposed function names match the module's dominant verb prefix
  2. async        — proposed functions follow the module's async/sync ratio
  3. sequential_awaits — proposed async functions don't have parallelizable awaits
  4. n_plus_one   — proposed functions don't issue DB queries inside loops

Score deductions per severity: high=0.25, medium=0.15, low=0.05. Clamped [0, 1].
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB

# ── Regex patterns (reused from performance.py conventions) ───────────────────

_VERB_RE = re.compile(r"^_*([a-z]+)_")

# DB sink patterns — same as performance._DB_SINK_PATTERNS
_DB_SINK_RE = re.compile(
    r"\b(?:_db\.execute|_pool\.acquire|conn\.fetch|conn\.execute"
    r"|asyncpg\.connect|aiosqlite\.connect)\b",
    re.IGNORECASE,
)

# Only match standalone for-loop statements, not list/dict/set comprehensions.
# performance.py's _LOOP_PATTERNS also matches comprehensions, but that works
# because it's paired with the call graph (callee_map). Here we only have the
# raw body text, so we restrict to actual loop statements to avoid false
# positives like `[dict(r) for r in await cur.fetchall()]`.
_LOOP_RE = re.compile(
    r"^\s*for\s+[\w,\s(]+\s+in\b",
    re.MULTILINE,
)

_SEVERITY_DEDUCTION = {"high": 0.25, "medium": 0.15, "low": 0.05}


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class Deviation:
    check: str       # "naming" | "async" | "sequential_awaits" | "n_plus_one"
    severity: str    # "high" | "medium" | "low"
    message: str
    example: str = ""

    def to_dict(self) -> dict:
        d = {"check": self.check, "severity": self.severity, "message": self.message}
        if self.example:
            d["example"] = self.example
        return d


@dataclass
class ValidationResult:
    conformance_score: float
    proposed_functions: list[str]
    module: str
    deviations: list[Deviation]
    active_constraints: list[str]

    def to_dict(self) -> dict:
        return {
            "conformance_score": round(self.conformance_score, 3),
            "module": self.module,
            "proposed_functions": self.proposed_functions,
            "deviations": [d.to_dict() for d in self.deviations],
            "active_constraints": self.active_constraints,
        }


# ── Pure check functions ──────────────────────────────────────────────────────

def _bare_name(name: str) -> str:
    """Strip class prefix from 'ClassName.method_name' → 'method_name'."""
    return name.split(".")[-1] if "." in name else name


def _dominant_verb(function_names: list[str]) -> tuple[str, float] | None:
    """Return (verb, ratio) if >60% of names share a verb prefix, else None."""
    verbs: Counter = Counter()
    for name in function_names:
        m = _VERB_RE.match(_bare_name(name))
        if m:
            verbs[m.group(1)] += 1
    if not verbs or not function_names:
        return None
    verb, cnt = verbs.most_common(1)[0]
    ratio = cnt / len(function_names)
    return (verb, ratio) if ratio >= 0.6 else None


def _check_naming(
    proposed: list[dict],
    existing: list[dict],
) -> Deviation | None:
    """Return a naming deviation if proposed functions break the module's verb convention."""
    if not existing or not proposed:
        return None

    existing_names = [n["name"] for n in existing]
    result = _dominant_verb(existing_names)
    if result is None:
        return None

    dominant_verb, _ = result
    violators = [
        p["name"] for p in proposed
        if not _VERB_RE.match(_bare_name(p["name"]))
        or _VERB_RE.match(_bare_name(p["name"])).group(1) != dominant_verb  # type: ignore[union-attr]
    ]
    if not violators:
        return None

    examples = ", ".join(n for n in existing_names[:4] if _VERB_RE.match(_bare_name(n)))
    return Deviation(
        check="naming",
        severity="medium",
        message=(
            f"{', '.join(f'`{v}`' for v in violators[:3])} "
            f"{'do' if len(violators) > 1 else 'does'} not follow the module's "
            f"`{dominant_verb}_*` naming convention"
        ),
        example=f"Existing: {examples}",
    )


def _check_async(
    proposed: list[dict],
    existing: list[dict],
) -> Deviation | None:
    """Return an async deviation if proposed functions break the module's async convention.

    Skips when all existing nodes have structural_layer='generic': their
    is_async=False reflects data absence, not a real sync convention. Firing
    on them would produce false "module is 0% async" positives when indexing
    transitions from generic to precision parsing.
    """
    if not existing or not proposed:
        return None

    # If every existing node is generically parsed, is_async=False is an
    # artefact of the parser, not evidence of a sync-first module.
    if all(n.get("structural_layer", "precision") == "generic" for n in existing):
        return None

    total_existing = len(existing)
    async_existing = sum(1 for n in existing if n.get("is_async"))
    ratio = async_existing / total_existing

    # Only flag when the module is clearly async-first (>0.7) or sync-first (<0.3)
    if ratio > 0.7:
        sync_proposed = [p["name"] for p in proposed if not p.get("is_async")]
        if not sync_proposed:
            return None
        example_async = next(
            (n["name"] for n in existing if n.get("is_async")), ""
        )
        return Deviation(
            check="async",
            severity="high",
            message=(
                f"Module is async-first ({async_existing}/{total_existing} existing functions). "
                f"{', '.join(f'`{n}`' for n in sync_proposed[:3])} "
                f"{'are' if len(sync_proposed) > 1 else 'is'} synchronous"
            ),
            example=f"Convention: async def {_bare_name(example_async)}(...)" if example_async else "",
        )

    if ratio < 0.3:
        async_proposed = [p["name"] for p in proposed if p.get("is_async")]
        if not async_proposed:
            return None
        example_sync = next(
            (n["name"] for n in existing if not n.get("is_async")), ""
        )
        return Deviation(
            check="async",
            severity="medium",
            message=(
                f"Module is sync-first ({total_existing - async_existing}/{total_existing} "
                f"existing functions are sync). "
                f"{', '.join(f'`{n}`' for n in async_proposed[:3])} "
                f"{'are' if len(async_proposed) > 1 else 'is'} async"
            ),
            example=f"Convention: def {_bare_name(example_sync)}(...)" if example_sync else "",
        )

    return None


def _check_sequential_awaits_in_proposed(
    proposed: list[dict],
) -> Deviation | None:
    """Return a deviation if any proposed async function has parallelizable sequential awaits."""
    from .performance import _detect_sequential_awaits

    for fn in proposed:
        if not fn.get("is_async"):
            continue
        detail = _detect_sequential_awaits(fn.get("body", ""))
        if detail:
            return Deviation(
                check="sequential_awaits",
                severity="high",
                message=f"`{fn['name']}`: {detail}",
                example="Fix: a, b = await asyncio.gather(get_x(), get_y())",
            )
    return None


def _check_db_in_loop(proposed: list[dict]) -> Deviation | None:
    """Return a deviation if any proposed function issues DB queries inside a loop."""
    for fn in proposed:
        body = fn.get("body", "")
        if _LOOP_RE.search(body) and _DB_SINK_RE.search(body):
            return Deviation(
                check="n_plus_one",
                severity="high",
                message=(
                    f"`{fn['name']}` issues a DB query inside a loop — "
                    "O(n) queries instead of O(1). "
                    "Pass all IDs at once with ANY($1) or executemany()."
                ),
                example="Fix: await db.execute('SELECT * FROM t WHERE id=ANY($1)', (ids,))",
            )
    return None


# ── Entry point ───────────────────────────────────────────────────────────────

def _score(deviations: list[Deviation]) -> float:
    total = sum(_SEVERITY_DEDUCTION.get(d.severity, 0.0) for d in deviations)
    return max(0.0, 1.0 - total)


async def validate_proposed_code(
    code: str,
    target_file: str,
    project_id: str,
    db: "CallGraphDB",
) -> ValidationResult:
    """
    Parse proposed code, compare against the indexed module, and return
    a conformance score with specific deviations.

    target_file determines the language (by extension) and which existing
    functions to compare against. If the file isn't indexed yet, naming
    and async checks are skipped — only performance checks run.
    """
    from .call_graph.parser import TreeSitterParser
    from .guidance import _contract_constraints

    # Parse proposed code in-memory
    parser = TreeSitterParser()
    proposed_nodes, _ = parser.parse_file(target_file, code)

    proposed: list[dict] = [
        {
            "name": n.name,
            "is_async": n.is_async,
            "body": n.body,
            "signature": n.signature,
        }
        for n in proposed_nodes
        if not n.is_external
    ]
    proposed_names = [p["name"] for p in proposed]

    # Derive module from first parsed node, fallback to path-based guess
    module = proposed_nodes[0].module if proposed_nodes else (
        Path(target_file).with_suffix("").as_posix().replace("/", ".")
    )

    # Fetch existing functions and contracts concurrently
    import asyncio as _asyncio
    existing, contracts = await _asyncio.gather(
        db.get_nodes_by_file(target_file, project_id),
        db.list_contracts(project_id),
    )

    # Run checks
    deviations: list[Deviation] = []

    naming_dev = _check_naming(proposed, existing)
    if naming_dev:
        deviations.append(naming_dev)

    async_dev = _check_async(proposed, existing)
    if async_dev:
        deviations.append(async_dev)

    seq_dev = _check_sequential_awaits_in_proposed(proposed)
    if seq_dev:
        deviations.append(seq_dev)

    loop_dev = _check_db_in_loop(proposed)
    if loop_dev:
        deviations.append(loop_dev)

    # Surface active contracts as constraints (not deviations)
    result_ids = [f"{module}.{p['name']}" for p in proposed]
    constraints = _contract_constraints(contracts, project_id, result_ids, [module])

    return ValidationResult(
        conformance_score=_score(deviations),
        proposed_functions=proposed_names,
        module=module,
        deviations=deviations,
        active_constraints=constraints,
    )
