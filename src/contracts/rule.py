from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ContractRule:
    """
    Pure representation of one contract's structural enforcement logic.
    No I/O — all methods take plain lists and return plain lists/bools.
    Extracted from ContractManager so the rule logic can be tested without a database.
    """

    prohibited_patterns: list[str]       # lower-cased callee name fragments that are forbidden
    required_callee: str | None          # if set, forbidden call is allowed when this is also called
    scope_exclusions: list[str]          # function ID prefixes exempt from checking
    missing_metadata: list[str]          # ["docstring"] triggers a PRESENCE check

    @classmethod
    def from_expr(cls, expr: dict) -> "ContractRule":
        return cls(
            prohibited_patterns=[p.lower() for p in expr.get("prohibited_patterns", [])],
            required_callee=expr.get("required_callee"),
            scope_exclusions=[s.lower() for s in expr.get("scope_exclusions", [])],
            missing_metadata=expr.get("missing_metadata", []),
        )

    def is_excluded(self, function_id: str) -> bool:
        return any(function_id.lower().startswith(ex) for ex in self.scope_exclusions)

    def excluded_names(self) -> set[str]:
        return {s.lower() for s in self.scope_exclusions}

    def find_prohibited_callees(self, callee_ids: list[str]) -> list[str]:
        """Return callee IDs that match a prohibited pattern, respecting required_callee."""
        if not self.prohibited_patterns:
            return []
        callee_names = [c.split(".")[-1].lower() for c in callee_ids]
        if self.required_callee:
            uses_required = any(self.required_callee.lower() in c.lower() for c in callee_ids)
            if uses_required:
                return []
        hits = []
        for pattern in self.prohibited_patterns:
            for cid, name in zip(callee_ids, callee_names):
                if name == pattern or name.startswith(pattern + "_") or name.endswith("_" + pattern):
                    hits.append(cid)
        return hits

    def needs_call_graph_check(self) -> bool:
        return bool(self.prohibited_patterns or self.required_callee)

    def needs_metadata_check(self) -> bool:
        return "docstring" in self.missing_metadata
