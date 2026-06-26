"""
SOLID principle violation detector for Scopenos.

Three structural detectors — no LLM calls, no embeddings required:

  SRP — Single Responsibility Principle
      Detects functions whose callees span 3+ unrelated subsystems.
      A function that calls into auth, storage, AND email has three independent
      reasons to change — any of those subsystems can break it.

  OCP — Open/Closed Principle
      Detects isinstance / type-dispatch fan-out in function bodies.
      A function with 3+ isinstance checks must be reopened every time a new
      type is introduced — the opposite of "closed for modification."

  DIP — Dependency Inversion Principle
      Detects non-infrastructure functions that directly call raw DB-layer
      functions in a different subsystem, skipping the abstraction boundary.
      Business logic calling concrete storage internals couples both to the
      same schema — a schema change breaks your business logic.

Suppression: call dismiss_solid_concern to log a SOLID-typed decision for
any finding you have reviewed and accepted. Future runs will show it as
status="acknowledged" so context is preserved without re-surfacing the noise.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .call_graph.storage import CallGraphDB


# ── Finding ───────────────────────────────────────────────────────────────────

@dataclass
class SolidFinding:
    function_id: str
    function_name: str
    file: str
    principle: str       # "SRP" | "OCP" | "DIP"
    severity: str        # "high" | "medium"
    detail: str
    suppressed: bool = False
    suppression_reason: str = ""

    def to_dict(self) -> dict:
        d = {
            "function": self.function_name,
            "file": self.file,
            "principle": self.principle,
            "severity": self.severity,
            "detail": self.detail,
        }
        if self.suppressed:
            d["status"] = "acknowledged"
            d["acknowledged_reason"] = self.suppression_reason
        else:
            d["status"] = "new"
        return d


# ── Subsystem helpers ─────────────────────────────────────────────────────────

def _subsystem(node_id: str) -> str:
    """First two dot-segments: 'src.call_graph.storage.upsert_nodes' → 'src.call_graph'."""
    parts = node_id.split(".")
    return ".".join(parts[:2]) if len(parts) >= 2 else parts[0]


_INFRA_RE = re.compile(
    r"\b(storage|repository|database|embedder|embed|cache|queue|redis|smtp|email_sender)\b",
    re.IGNORECASE,
)


def _is_infrastructure(node_id: str) -> bool:
    """Heuristic: does this node live in a storage/infrastructure module?"""
    return bool(_INFRA_RE.search(node_id))


# ── SRP detector ──────────────────────────────────────────────────────────────

def detect_srp_violation(
    node_id: str,
    callee_ids: list[str],
    nodes_by_id: dict[str, dict],
) -> tuple[str | None, str | None]:
    """
    Return (severity, detail) if this function's callees span too many
    unrelated subsystems. Returns (None, None) if no violation.
    """
    caller_sub = _subsystem(node_id)
    foreign_subs: set[str] = set()
    for callee_id in callee_ids:
        # Skip unresolved callee names (builtins, externals) — they only inflate the count
        if callee_id not in nodes_by_id:
            continue
        if nodes_by_id[callee_id].get("is_external"):
            continue
        sub = _subsystem(callee_id)
        if sub != caller_sub:
            foreign_subs.add(sub)

    if len(foreign_subs) >= 4:
        listed = ", ".join(sorted(foreign_subs)[:5])
        return "high", (
            f"Calls into {len(foreign_subs)} unrelated subsystems: {listed}. "
            f"This function has {len(foreign_subs)} independent reasons to change — "
            f"consider decomposing into focused collaborators."
        )
    if len(foreign_subs) == 3:
        listed = ", ".join(sorted(foreign_subs))
        return "medium", (
            f"Calls into 3 unrelated subsystems: {listed}. "
            f"Each subsystem change can cascade here — a sign of mixed responsibilities."
        )
    return None, None


# ── OCP detector ──────────────────────────────────────────────────────────────

# isinstance(x, SomeType)                      — single-type form
_ISINSTANCE_SINGLE_RE = re.compile(r"\bisinstance\s*\(\s*\w[\w.]*\s*,\s*([\w.]+)", re.MULTILINE)
# isinstance(x, (TypeA, TypeB, TypeC))         — tuple form: capture each name inside parens
_ISINSTANCE_TUPLE_RE = re.compile(r"\bisinstance\s*\(\s*\w[\w.]*\s*,\s*\(([^)]+)\)", re.MULTILINE)
# type(x) == SomeType or type(x) is SomeType
_TYPE_CMP_RE = re.compile(r"\btype\s*\(\s*\w[\w.]*\s*\)\s*(?:==|is)\s*([\w.]+)", re.MULTILINE)

# Common validation types that appear in isinstance for input-checking, not dispatch
_VALIDATION_TYPES = frozenset({
    "str", "int", "float", "bool", "list", "dict", "tuple",
    "bytes", "set", "None", "NoneType", "type",
})


def detect_ocp_violation(body: str) -> tuple[str | None, str | None]:
    """
    Return (severity, detail) if the function body type-dispatches on many
    concrete types via isinstance — a sign that adding a new type requires
    modifying this function.
    Returns (None, None) if no violation.
    """
    types: set[str] = set(_ISINSTANCE_SINGLE_RE.findall(body))
    types |= set(_TYPE_CMP_RE.findall(body))
    # Expand tuple-form isinstance: isinstance(x, (A, B, C)) → A, B, C
    for match in _ISINSTANCE_TUPLE_RE.finditer(body):
        for name in re.split(r"[\s,]+", match.group(1)):
            name = name.strip()
            if name:
                types.add(name)
    dispatch_types = types - _VALIDATION_TYPES

    if len(dispatch_types) >= 5:
        listed = ", ".join(sorted(dispatch_types)[:6])
        return "high", (
            f"Dispatches on {len(dispatch_types)} concrete types via isinstance: {listed}. "
            f"Every new type requires reopening this function — "
            f"replace with polymorphism or a handler registry."
        )
    if len(dispatch_types) >= 3:
        listed = ", ".join(sorted(dispatch_types))
        return "medium", (
            f"Dispatches on {len(dispatch_types)} concrete types via isinstance: {listed}. "
            f"Consider extracting a protocol or registry so new types extend "
            f"behavior without modifying this function."
        )
    return None, None


# ── DIP detector ──────────────────────────────────────────────────────────────

# Raw infrastructure access patterns — these should only appear inside storage/infra modules
_RAW_INFRA_RE = re.compile(
    r"\b(?:_db\.execute|_db\._db|_pool\.acquire|conn\.fetch|conn\.execute"
    r"|asyncpg\.connect|aiosqlite\.connect)\b",
    re.IGNORECASE,
)


def detect_dip_violation(
    node_id: str,
    body: str,
) -> tuple[str | None, str | None]:
    """
    Return (severity, detail) if a non-infrastructure function's body directly
    uses raw DB/infrastructure access patterns, bypassing the abstraction layer.

    This catches the case where business logic reaches past the repository/service
    boundary to touch asyncpg, connection pools, or internal _db attributes directly.
    Returns (None, None) if no violation.
    """
    if _is_infrastructure(node_id):
        return None, None

    matches = list(dict.fromkeys(_RAW_INFRA_RE.findall(body)))  # unique, order-preserving
    if len(matches) >= 2:
        listed = ", ".join(matches[:3])
        return "high", (
            f"Non-infrastructure function directly accesses raw infrastructure: {listed}. "
            f"Business logic should call an abstraction layer, not reach past it "
            f"to connection pools or internal _db attributes."
        )
    if matches:
        return "medium", (
            f"Non-infrastructure function directly accesses raw infrastructure: {matches[0]}. "
            f"Introduce an abstraction boundary to insulate from infrastructure changes."
        )
    return None, None


# ── Main entry points ─────────────────────────────────────────────────────────

async def check_solid(
    db: "CallGraphDB",
    project_id: str,
) -> list[SolidFinding]:
    """
    Run SRP, OCP, and DIP detectors against the indexed functions for project_id.
    Returns SolidFinding objects, with suppressed=True for acknowledged violations.

    I/O is isolated here; all detection logic is in _run_solid_detectors (pure).
    """
    nodes_by_id = await db.get_nodes_with_bodies(project_id)
    callee_map = await db.get_callee_map(project_id)
    acknowledged = await db.get_acknowledged_decisions_by_type(project_id, "SOLID")
    return _run_solid_detectors(nodes_by_id, callee_map, acknowledged)


def _run_solid_detectors(
    nodes_by_id: dict[str, dict],
    callee_map: dict[str, list[str]],
    acknowledged: dict[str, str],
) -> list[SolidFinding]:
    """Pure detection pipeline — no I/O. Accepts pre-loaded data from check_solid.

    Separated from check_solid so callers that already hold the data dicts
    (e.g. tests) can invoke detection directly without a database.
    """
    findings: list[SolidFinding] = []

    for node_id, node in nodes_by_id.items():
        if node.get("is_external") or node.get("type") in ("class", "ClassDef"):
            continue

        callee_ids = callee_map.get(node_id, [])
        name = node.get("name", node_id)
        file_ = node.get("file", "")
        body = node.get("body", "")
        suppressed = node_id in acknowledged
        reason = acknowledged.get(node_id, "")

        sev, detail = detect_srp_violation(node_id, callee_ids, nodes_by_id)
        if sev:
            findings.append(SolidFinding(
                function_id=node_id, function_name=name, file=file_,
                principle="SRP", severity=sev, detail=detail,
                suppressed=suppressed, suppression_reason=reason,
            ))

        sev, detail = detect_ocp_violation(body)
        if sev:
            findings.append(SolidFinding(
                function_id=node_id, function_name=name, file=file_,
                principle="OCP", severity=sev, detail=detail,
                suppressed=suppressed, suppression_reason=reason,
            ))

        sev, detail = detect_dip_violation(node_id, body)
        if sev:
            findings.append(SolidFinding(
                function_id=node_id, function_name=name, file=file_,
                principle="DIP", severity=sev, detail=detail,
                suppressed=suppressed, suppression_reason=reason,
            ))

    _sev = {"high": 0, "medium": 1}
    findings.sort(key=lambda f: (f.suppressed, _sev.get(f.severity, 9), f.principle))
    return findings
