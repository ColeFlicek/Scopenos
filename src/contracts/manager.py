from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from anthropic import AsyncAnthropic

from .rule import ContractRule

DRAFT_MODEL = "claude-haiku-4-5-20251001"


class ContractManager:
    """
    Manages the full lifecycle of Invariant Contracts:
    - Draft generation (LLM parses natural language → violation/compliance examples)
    - Approval (activates + embeds examples)
    - Checking (structural call-graph check + semantic embedding check)
    """

    def __init__(self, db, embeddings) -> None:
        """Wire up database, embeddings store, and Anthropic client."""
        self._db = db
        self._embeddings = embeddings
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # ── Draft generation ───────────────────────────────────────────────────

    async def generate_draft(
        self,
        project_ids: list[str],
        title: str,
        natural_language: str,
        function_ids: list[str] | None = None,
    ) -> dict:
        """
        Call Claude Haiku to parse natural_language into:
        - violation_examples (4-5 code snippets that break the rule)
        - compliance_examples (2-3 code snippets that follow the rule)
        - structural_expression (JSON: prohibited_patterns, required_callee, scope_exclusions)
        - rule_type (SEMANTIC | BOUNDARY | PRESENCE)

        function_ids: optional list of function IDs to scope this contract to.
        When set, check_contracts only evaluates those functions instead of the
        entire project — use this to encode pattern-level invariants (e.g. all
        functions forming an Observer subsystem).

        Returns the saved draft contract with generated examples.
        """
        # Ground the LLM in actual callee names from this project's call graph.
        # Without this, the LLM generates abstract semantic descriptions instead of
        # real Python identifiers, and the structural checker never fires.
        callee_context = ""
        if project_ids:
            try:
                raw_ids = await self._db.get_distinct_callee_names(project_ids[0], limit=80)
                if raw_ids:
                    # Bare names (last segment) are what the rule engine matches on.
                    bare = sorted({cid.split(".")[-1] for cid in raw_ids if cid})
                    callee_context = (
                        f"\n\nActual function/method names called within this project "
                        f"(last segment of each call graph edge callee ID):\n{json.dumps(bare)}\n"
                        "When building prohibited_patterns, prefer names from this list "
                        "that match the type of access described in the rule."
                    )
            except Exception:
                pass  # Non-fatal: contract is still useful without grounding

        parsed = await self._llm_parse_contract(natural_language, callee_context=callee_context)
        contract_id = str(uuid.uuid4())

        await self._db.create_contract(
            contract_id=contract_id,
            project_ids=project_ids,
            title=title,
            natural_language=natural_language,
            rule_type=parsed.get("rule_type", "SEMANTIC"),
            structural_expression=json.dumps(parsed.get("structural_expression", {})),
            threshold=0.85,
            function_ids=function_ids,
        )

        def _to_str(c) -> str:
            """Coerce a contract example to a plain string — LLM may return dicts."""
            return c if isinstance(c, str) else json.dumps(c)

        examples = (
            [{"type": "violation", "code": _to_str(c)} for c in parsed.get("violation_examples", [])]
            + [{"type": "compliance", "code": _to_str(c)} for c in parsed.get("compliance_examples", [])]
        )
        await self._db.upsert_contract_examples(contract_id, examples)

        return await self._contract_with_examples(contract_id)

    async def _llm_parse_contract(self, natural_language: str, callee_context: str = "") -> dict:
        """Call Claude Haiku to parse a natural-language rule into structured contract fields."""
        prompt = f"""You are helping build a code contract enforcement system.

A user has written this architectural rule:
"{natural_language}"
{callee_context}
Your job is to extract structured information from this rule.

Return a JSON object with these fields:
- "rule_type": one of "SEMANTIC", "BOUNDARY", or "PRESENCE"
  - SEMANTIC: rule about what code patterns are allowed/forbidden
  - BOUNDARY: rule about which modules/layers may call which
  - PRESENCE: rule about whether specific metadata (e.g. docstrings, comments) must be present on all functions
- "structural_expression": a JSON object with:
  - "prohibited_patterns": CRITICAL — these must be REAL Python function or method name fragments
    (bare identifiers like "execute", "fetch", "raw_query") that actually appear in call graph edges.
    The rule engine does exact last-segment matching: "execute" matches any callee whose name IS
    "execute" or starts/ends with "_execute". Do NOT use abstract descriptions like
    "database_access_without_auth" — those will never match anything. If the project's actual
    callee names are provided above, prefer those. Leave as [] only if no specific call is forbidden.
  - "required_callee": the bare function name that MUST be called alongside any prohibited one
    (e.g. "require_user", "read_secrets"). The rule fires when a function calls a prohibited
    pattern WITHOUT also calling required_callee. Set to null if there is no required companion.
  - "scope_exclusions": list of function ID prefixes that are explicitly exempt from this rule.
    Use module path prefixes (e.g. "src.call_graph.storage", "api_signup") — the checker does
    prefix matching on full function IDs.
  - "missing_metadata": for PRESENCE rules only — list ["docstring"] if all functions must have
    docstrings. Leave as [] for non-PRESENCE rules.
- "violation_examples": list of 4-5 short Python code snippets (3-8 lines each) that VIOLATE this rule.
- "compliance_examples": list of 2-3 short Python code snippets (3-8 lines each) that CORRECTLY FOLLOW this rule.

Return ONLY the JSON object, no markdown, no explanation."""

        try:
            resp = await self._anthropic.messages.create(
                model=DRAFT_MODEL,
                max_tokens=1200,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text.strip()
            # Strip markdown code fences if present.
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
            return json.loads(text)
        except Exception as exc:
            print(f"[contracts] LLM parse failed ({exc}), returning empty structure")
            return {
                "rule_type": "SEMANTIC",
                "structural_expression": {
                    "prohibited_patterns": [],
                    "required_callee": None,
                    "scope_exclusions": [],
                },
                "violation_examples": [],
                "compliance_examples": [],
            }

    # ── Approval ────────────────────────────────────────────────────────────

    async def approve(self, contract_id: str) -> dict:
        """Activate a draft contract: embed examples and set status=active."""
        contract = await self._db.get_contract(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")

        examples = await self._db.list_contract_examples(contract_id)
        violation_codes = [e["code"] for e in examples if e["example_type"] == "violation"]
        compliance_codes = [e["code"] for e in examples if e["example_type"] == "compliance"]

        await self._embeddings.upsert_contract_embeddings(
            contract_id, violation_codes, compliance_codes
        )
        await self._db.update_contract_status(contract_id, "active")
        return await self._contract_with_examples(contract_id)

    async def update_structural_expression(
        self, contract_id: str, structural_expression: dict
    ) -> dict:
        """Replace the structural_expression of a contract.

        Useful for correcting LLM-generated abstract patterns with concrete
        function name fragments after a contract has been created.
        Does not change status — an active contract stays active.
        """
        contract = await self._db.get_contract(contract_id)
        if not contract:
            raise ValueError(f"Contract {contract_id} not found")
        await self._db.update_contract_structural(
            contract_id, json.dumps(structural_expression)
        )
        return await self._contract_with_examples(contract_id)

    async def deactivate(self, contract_id: str) -> None:
        """Set a contract's status back to draft, pausing enforcement."""
        await self._db.update_contract_status(contract_id, "draft")

    async def delete(self, contract_id: str) -> None:
        """Permanently delete a contract and all its embeddings."""
        await self._embeddings.delete_contract_embeddings(contract_id)
        await self._db.delete_contract(contract_id)

    # ── Example updates ────────────────────────────────────────────────────

    async def update_examples(
        self,
        contract_id: str,
        violation_examples: list[str],
        compliance_examples: list[str],
    ) -> dict:
        """Replace violation/compliance examples; re-embeds if contract is already active."""
        examples = (
            [{"type": "violation", "code": c} for c in violation_examples]
            + [{"type": "compliance", "code": c} for c in compliance_examples]
        )
        await self._db.upsert_contract_examples(contract_id, examples)
        # If contract was active, re-embed with the new examples.
        contract = await self._db.get_contract(contract_id)
        if contract and contract["status"] == "active":
            await self._embeddings.upsert_contract_embeddings(
                contract_id, violation_examples, compliance_examples
            )
        return await self._contract_with_examples(contract_id)

    # ── Checking ───────────────────────────────────────────────────────────

    async def check_project(self, project_id: str, semantic: bool = False) -> list[dict]:
        """Run all active contracts against a project's call graph and return all violations.

        semantic=False (default): structural call-graph checks only — fast, suitable for CI.
        semantic=True: also runs embedding-based semantic checks against every project
        function. Expensive for large projects — use on focused subsets or small codebases.
        """
        contracts = await self._db.list_contracts(project_id)
        active = [c for c in contracts if c["status"] == "active"]
        if not active:
            return []

        violations: list[dict] = []
        for contract in active:
            structural_expr = json.loads(contract["structural_expression"])
            scoped_ids = contract.get("function_ids") or []
            new_viols = await self._check_structural(
                contract["id"], project_id, structural_expr,
                function_ids=scoped_ids if scoped_ids else None,
            )
            violations.extend(new_viols)

        if semantic:
            nodes_by_id = await self._db.get_nodes_with_bodies(project_id)
            for fid, node in nodes_by_id.items():
                if not node.get("is_external"):
                    body = node.get("body", "") or ""
                    snippet = "\n".join(filter(None, [
                        node.get("signature", ""),
                        node.get("docstring", ""),
                        node.get("summary", ""),
                        body[:600],
                    ]))
                    if not snippet.strip():
                        continue
                    for contract in active:
                        is_viol, viol_score, comp_score = await self._embeddings.check_semantic(
                            contract["id"], snippet
                        )
                        if is_viol:
                            await self._db.log_violation(
                                contract_id=contract["id"],
                                function_id=fid,
                                project_id=project_id,
                                violation_type="semantic",
                                score=viol_score,
                            )
                            violations.append({
                                "contract_id": contract["id"],
                                "contract_title": contract["title"],
                                "function_id": fid,
                                "project_id": project_id,
                                "violation_type": "semantic",
                                "score": viol_score,
                                "compliance_score": comp_score,
                            })

        return violations

    async def check_functions(
        self, project_id: str, function_ids: list[str]
    ) -> list[dict]:
        """Check a specific set of function IDs against all active contracts (used by the post-commit hook)."""
        contracts = await self._db.list_contracts(project_id)
        active = [c for c in contracts if c["status"] == "active"]
        if not active or not function_ids:
            return []

        violations: list[dict] = []
        for contract in active:
            rule = ContractRule.from_expr(json.loads(contract["structural_expression"]))
            for fid in function_ids:
                # Structural check for this specific function.
                viols = await self._check_structural_for_function(
                    contract["id"], project_id, fid, rule
                )
                violations.extend(viols)

                # Semantic check: embed the function's body text.
                node = await self._db.get_node(fid, project_id)
                if node:
                    body = node.get("body", "") or ""
                    snippet = "\n".join(filter(None, [
                        node.get("signature", ""),
                        node.get("docstring", ""),
                        node.get("summary", ""),
                        body[:600],
                    ]))
                    is_viol, viol_score, comp_score = await self._embeddings.check_semantic(
                        contract["id"], snippet
                    )
                    if is_viol:
                        await self._db.log_violation(
                            contract_id=contract["id"],
                            function_id=fid,
                            project_id=project_id,
                            violation_type="semantic",
                            score=viol_score,
                        )
                        violations.append({
                            "contract_id": contract["id"],
                            "contract_title": contract["title"],
                            "function_id": fid,
                            "project_id": project_id,
                            "violation_type": "semantic",
                            "score": viol_score,
                            "compliance_score": comp_score,
                        })
        return violations

    async def _check_structural(
        self, contract_id: str, project_id: str, expr: dict,
        function_ids: list[str] | None = None,
    ) -> list[dict]:
        """Scan project functions for structural violations via call-graph traversal.

        function_ids: when provided, only these functions are checked (pattern-scoped
        contracts). When None, the entire project is scanned.
        """
        rule = ContractRule.from_expr(expr)
        violations: list[dict] = []

        if rule.needs_metadata_check():
            rows = await self._db.get_nodes_missing_docstring(
                project_id, exclude_names=rule.excluded_names()
            )
            for row in rows:
                if function_ids is not None and row["id"] not in function_ids:
                    continue
                await self._db.log_violation(
                    contract_id=contract_id,
                    function_id=row["id"],
                    project_id=project_id,
                    violation_type="missing_docstring",
                    score=1.0,
                )
                violations.append({
                    "contract_id": contract_id,
                    "function_id": row["id"],
                    "project_id": project_id,
                    "violation_type": "missing_docstring",
                    "score": 1.0,
                })

        if not rule.needs_call_graph_check():
            return violations

        caller_ids = function_ids if function_ids is not None else await self._db.get_all_caller_ids(project_id)
        for caller_id in caller_ids:
            if rule.is_excluded(caller_id):
                continue
            viols = await self._check_structural_for_function(
                contract_id, project_id, caller_id, rule
            )
            violations.extend(viols)
        return violations

    async def _check_structural_for_function(
        self,
        contract_id: str,
        project_id: str,
        function_id: str,
        rule: ContractRule,
        depth: int = 2,
    ) -> list[dict]:
        """Check one function against a ContractRule, BFS up to `depth` callee levels.

        depth=2 catches one-wrapper bypasses: fn → helper → prohibited_callee.
        Direct violations (depth 1) and transitive violations (depth 2) are both
        reported against the original function_id.
        """
        if rule.is_excluded(function_id) or not rule.needs_call_graph_check():
            return []

        # BFS: collect all callee IDs up to `depth` hops from function_id
        visited: set[str] = {function_id}
        frontier: list[str] = [function_id]
        callee_ids: list[str] = []
        for _ in range(depth):
            next_frontier: list[str] = []
            for fid in frontier:
                callees = await self._db.get_callees(fid, project_id)
                for c in callees:
                    cid = c["id"]
                    if cid not in visited:
                        visited.add(cid)
                        callee_ids.append(cid)
                        next_frontier.append(cid)
            frontier = next_frontier
            if not frontier:
                break

        matching = rule.find_prohibited_callees(callee_ids)

        if not matching:
            return []

        await self._db.log_violation(
            contract_id=contract_id,
            function_id=function_id,
            project_id=project_id,
            violation_type="structural",
            score=1.0,
        )
        return [{
            "contract_id": contract_id,
            "function_id": function_id,
            "project_id": project_id,
            "violation_type": "structural",
            "score": 1.0,
            "matching_callees": matching,
        }]

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _contract_with_examples(self, contract_id: str) -> dict:
        """Fetch a contract and attach its violation/compliance example lists."""
        contract = await self._db.get_contract(contract_id)
        if not contract:
            return {}
        examples = await self._db.list_contract_examples(contract_id)
        contract["violation_examples"] = [e["code"] for e in examples if e["example_type"] == "violation"]
        contract["compliance_examples"] = [e["code"] for e in examples if e["example_type"] == "compliance"]
        return contract

    async def list_contracts(self, project_id: str | None = None) -> list[dict]:
        """Return all contracts with examples attached, optionally filtered to a project."""
        contracts = await self._db.list_contracts(project_id)
        result = []
        for c in contracts:
            examples = await self._db.list_contract_examples(c["id"])
            c["violation_examples"] = [e["code"] for e in examples if e["example_type"] == "violation"]
            c["compliance_examples"] = [e["code"] for e in examples if e["example_type"] == "compliance"]
            result.append(c)
        return result
