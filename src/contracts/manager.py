from __future__ import annotations

import json
import os
import re
import uuid
from typing import Any

from anthropic import AsyncAnthropic

DRAFT_MODEL = "claude-haiku-4-5-20251001"


class ContractManager:
    """
    Manages the full lifecycle of Invariant Contracts:
    - Draft generation (LLM parses natural language → violation/compliance examples)
    - Approval (activates + embeds examples)
    - Checking (structural call-graph check + semantic embedding check)
    """

    def __init__(self, db, embeddings) -> None:
        self._db = db
        self._embeddings = embeddings
        self._anthropic = AsyncAnthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    # ── Draft generation ───────────────────────────────────────────────────

    async def generate_draft(
        self,
        project_ids: list[str],
        title: str,
        natural_language: str,
    ) -> dict:
        """
        Call Claude Haiku to parse natural_language into:
        - violation_examples (4-5 code snippets that break the rule)
        - compliance_examples (2-3 code snippets that follow the rule)
        - structural_expression (JSON: prohibited_patterns, required_callee, scope_exclusions)
        - rule_type (SEMANTIC | BOUNDARY | PRESENCE)

        Returns the saved draft contract with generated examples.
        """
        parsed = await self._llm_parse_contract(natural_language)
        contract_id = str(uuid.uuid4())

        await self._db.create_contract(
            contract_id=contract_id,
            project_ids=project_ids,
            title=title,
            natural_language=natural_language,
            rule_type=parsed.get("rule_type", "SEMANTIC"),
            structural_expression=json.dumps(parsed.get("structural_expression", {})),
            threshold=0.85,
        )

        def _to_str(c) -> str:
            return c if isinstance(c, str) else json.dumps(c)

        examples = (
            [{"type": "violation", "code": _to_str(c)} for c in parsed.get("violation_examples", [])]
            + [{"type": "compliance", "code": _to_str(c)} for c in parsed.get("compliance_examples", [])]
        )
        await self._db.upsert_contract_examples(contract_id, examples)

        return await self._contract_with_examples(contract_id)

    async def _llm_parse_contract(self, natural_language: str) -> dict:
        prompt = f"""You are helping build a code contract enforcement system.

A user has written this architectural rule:
"{natural_language}"

Your job is to extract structured information from this rule.

Return a JSON object with these fields:
- "rule_type": one of "SEMANTIC", "BOUNDARY", or "PRESENCE"
  - SEMANTIC: rule about what code patterns are allowed/forbidden
  - BOUNDARY: rule about which modules/layers may call which
  - PRESENCE: rule about whether a specific function/pattern must or must not exist
- "structural_expression": a JSON object with:
  - "prohibited_patterns": list of function/method names that are forbidden (e.g. ["execute", "raw_query"])
  - "required_callee": the function that MUST be used instead (e.g. "read_secrets"), or null
  - "scope_exclusions": module prefixes that are exempt from the rule (e.g. ["auth", "db.internal"])
- "violation_examples": list of 4-5 short Python code snippets (3-8 lines each) that VIOLATE this rule. Make them look realistic — different ways to express the same violation.
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

    async def deactivate(self, contract_id: str) -> None:
        await self._db.update_contract_status(contract_id, "draft")

    async def delete(self, contract_id: str) -> None:
        await self._embeddings.delete_contract_embeddings(contract_id)
        await self._db.delete_contract(contract_id)

    # ── Example updates ────────────────────────────────────────────────────

    async def update_examples(
        self,
        contract_id: str,
        violation_examples: list[str],
        compliance_examples: list[str],
    ) -> dict:
        """Replace examples on a draft contract (before approval)."""
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

    async def check_project(self, project_id: str) -> list[dict]:
        """
        Run all active contracts against the call graph and indexed functions of a project.
        Returns list of violation dicts.
        """
        contracts = await self._db.list_contracts(project_id)
        active = [c for c in contracts if c["status"] == "active"]
        if not active:
            return []

        violations: list[dict] = []
        for contract in active:
            structural_expr = json.loads(contract["structural_expression"])
            new_viols = await self._check_structural(
                contract["id"], project_id, structural_expr
            )
            violations.extend(new_viols)
        return violations

    async def check_functions(
        self, project_id: str, function_ids: list[str]
    ) -> list[dict]:
        """
        Check a specific set of function IDs against all active contracts.
        Used by post-commit hook to check only newly changed functions.
        """
        contracts = await self._db.list_contracts(project_id)
        active = [c for c in contracts if c["status"] == "active"]
        if not active or not function_ids:
            return []

        violations: list[dict] = []
        for contract in active:
            structural_expr = json.loads(contract["structural_expression"])
            for fid in function_ids:
                # Structural check for this specific function.
                viols = await self._check_structural_for_function(
                    contract["id"], project_id, fid, structural_expr
                )
                violations.extend(viols)

                # Semantic check: embed the function's body text.
                node = await self._db.get_node(fid, project_id)
                if node:
                    # Use signature + docstring for semantic check — more concrete than
                    # the one-sentence summary and closer to the violation example patterns.
                    snippet = "\n".join(filter(None, [
                        node.get("signature", ""),
                        node.get("docstring", ""),
                        node.get("summary", ""),
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
        self, contract_id: str, project_id: str, expr: dict
    ) -> list[dict]:
        """Check ALL functions in a project for structural violations."""
        prohibited = [p.lower() for p in expr.get("prohibited_patterns", [])]
        required_callee = expr.get("required_callee")
        scope_exclusions = [s.lower() for s in expr.get("scope_exclusions", [])]
        if not prohibited and not required_callee:
            return []

        violations = []
        # Get all edges in the project.
        async with self._db._db.execute(
            "SELECT DISTINCT caller_id FROM edges WHERE project_id = ?", (project_id,)
        ) as cur:
            caller_ids = [row[0] for row in await cur.fetchall()]

        for caller_id in caller_ids:
            if any(caller_id.lower().startswith(ex) for ex in scope_exclusions):
                continue
            viols = await self._check_structural_for_function(
                contract_id, project_id, caller_id, expr
            )
            violations.extend(viols)
        return violations

    async def _check_structural_for_function(
        self, contract_id: str, project_id: str, function_id: str, expr: dict
    ) -> list[dict]:
        """Check a single function for structural violations."""
        prohibited = [p.lower() for p in expr.get("prohibited_patterns", [])]
        required_callee = expr.get("required_callee")
        scope_exclusions = [s.lower() for s in expr.get("scope_exclusions", [])]

        if any(function_id.lower().startswith(ex) for ex in scope_exclusions):
            return []

        if not prohibited and not required_callee:
            return []

        # Get all callees of this function.
        async with self._db._db.execute(
            "SELECT callee_id FROM edges WHERE caller_id = ? AND project_id = ?",
            (function_id, project_id),
        ) as cur:
            callee_ids = [row[0] for row in await cur.fetchall()]

        violations = []
        callee_names = [c.split(".")[-1].lower() for c in callee_ids]

        for pattern in prohibited:
            matching_callees = [
                c for c, name in zip(callee_ids, callee_names)
                if name == pattern or name.startswith(pattern + "_") or name.endswith("_" + pattern)
            ]
            # If the required callee is also present, it's compliant.
            if required_callee:
                uses_required = any(
                    required_callee.lower() in c.lower() for c in callee_ids
                )
                if uses_required:
                    continue  # correct path used
            if matching_callees:
                await self._db.log_violation(
                    contract_id=contract_id,
                    function_id=function_id,
                    project_id=project_id,
                    violation_type="structural",
                    score=1.0,
                )
                violations.append({
                    "contract_id": contract_id,
                    "function_id": function_id,
                    "project_id": project_id,
                    "violation_type": "structural",
                    "score": 1.0,
                    "matching_callees": matching_callees,
                })
        return violations

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _contract_with_examples(self, contract_id: str) -> dict:
        contract = await self._db.get_contract(contract_id)
        if not contract:
            return {}
        examples = await self._db.list_contract_examples(contract_id)
        contract["violation_examples"] = [e["code"] for e in examples if e["example_type"] == "violation"]
        contract["compliance_examples"] = [e["code"] for e in examples if e["example_type"] == "compliance"]
        return contract

    async def list_contracts(self, project_id: str | None = None) -> list[dict]:
        contracts = await self._db.list_contracts(project_id)
        result = []
        for c in contracts:
            examples = await self._db.list_contract_examples(c["id"])
            c["violation_examples"] = [e["code"] for e in examples if e["example_type"] == "violation"]
            c["compliance_examples"] = [e["code"] for e in examples if e["example_type"] == "compliance"]
            result.append(c)
        return result
