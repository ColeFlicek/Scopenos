"""
Tests for ContractRule — the pure predicate class extracted from ContractManager.

Every test here uses plain Python lists and dicts. No database, no async,
no mocking. If a test needs infrastructure to run, the design is wrong.
"""
import pytest
from src.contracts.rule import ContractRule


# ── from_expr ─────────────────────────────────────────────────────────────────

class TestFromExpr:
    def test_parses_prohibited_patterns(self):
        rule = ContractRule.from_expr({"prohibited_patterns": ["Execute", "RAW_QUERY"]})
        assert rule.prohibited_patterns == ["execute", "raw_query"]

    def test_lowercases_all_fields(self):
        rule = ContractRule.from_expr({
            "prohibited_patterns": ["FORBIDDEN"],
            "scope_exclusions": ["SRC.Tests"],
        })
        assert rule.prohibited_patterns == ["forbidden"]
        assert rule.scope_exclusions == ["src.tests"]

    def test_empty_expr_produces_empty_rule(self):
        rule = ContractRule.from_expr({})
        assert rule.prohibited_patterns == []
        assert rule.required_callee is None
        assert rule.scope_exclusions == []
        assert rule.missing_metadata == []

    def test_required_callee_preserved(self):
        rule = ContractRule.from_expr({"required_callee": "read_secrets"})
        assert rule.required_callee == "read_secrets"

    def test_missing_metadata_preserved(self):
        rule = ContractRule.from_expr({"missing_metadata": ["docstring"]})
        assert rule.missing_metadata == ["docstring"]


# ── is_excluded ───────────────────────────────────────────────────────────────

class TestIsExcluded:
    def test_excluded_when_id_starts_with_exclusion(self):
        rule = ContractRule.from_expr({"scope_exclusions": ["src.tests"]})
        assert rule.is_excluded("src.tests.helpers.setup") is True

    def test_not_excluded_when_no_match(self):
        rule = ContractRule.from_expr({"scope_exclusions": ["src.tests"]})
        assert rule.is_excluded("src.server.list_projects") is False

    def test_case_insensitive_exclusion(self):
        rule = ContractRule.from_expr({"scope_exclusions": ["src.Tests"]})
        assert rule.is_excluded("src.tests.helpers") is True

    def test_empty_exclusions_never_excludes(self):
        rule = ContractRule.from_expr({})
        assert rule.is_excluded("anything.at.all") is False

    def test_exact_match_is_excluded(self):
        rule = ContractRule.from_expr({"scope_exclusions": ["src.call_graph"]})
        assert rule.is_excluded("src.call_graph") is True


# ── find_prohibited_callees ───────────────────────────────────────────────────

class TestFindProhibitedCallees:
    def test_returns_empty_when_no_prohibited_patterns(self):
        rule = ContractRule.from_expr({})
        callee_ids = ["src.db.execute", "src.db.fetch"]
        assert rule.find_prohibited_callees(callee_ids) == []

    def test_exact_name_match(self):
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute"]})
        callee_ids = ["src.db.execute", "src.db.fetch"]
        assert rule.find_prohibited_callees(callee_ids) == ["src.db.execute"]

    def test_prefix_match(self):
        # "execute_raw" starts with "execute_"
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute"]})
        callee_ids = ["src.db.execute_raw"]
        assert rule.find_prohibited_callees(callee_ids) == ["src.db.execute_raw"]

    def test_suffix_match(self):
        # "bulk_execute" ends with "_execute"
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute"]})
        callee_ids = ["src.db.bulk_execute"]
        assert rule.find_prohibited_callees(callee_ids) == ["src.db.bulk_execute"]

    def test_no_partial_substring_match(self):
        # "executor" contains "execute" but is not exact / prefix / suffix
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute"]})
        callee_ids = ["src.db.executor"]
        assert rule.find_prohibited_callees(callee_ids) == []

    def test_multiple_prohibited_patterns(self):
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute", "raw_query"]})
        callee_ids = ["src.db.execute", "src.db.raw_query", "src.db.fetch"]
        hits = rule.find_prohibited_callees(callee_ids)
        assert "src.db.execute" in hits
        assert "src.db.raw_query" in hits
        assert "src.db.fetch" not in hits

    def test_required_callee_present_clears_violations(self):
        # When required_callee is used, the prohibited call is forgiven.
        rule = ContractRule.from_expr({
            "prohibited_patterns": ["execute"],
            "required_callee": "read_secrets",
        })
        callee_ids = ["src.db.execute", "src.auth.read_secrets"]
        assert rule.find_prohibited_callees(callee_ids) == []

    def test_required_callee_absent_violations_reported(self):
        rule = ContractRule.from_expr({
            "prohibited_patterns": ["execute"],
            "required_callee": "read_secrets",
        })
        callee_ids = ["src.db.execute"]  # no read_secrets
        assert rule.find_prohibited_callees(callee_ids) == ["src.db.execute"]

    def test_empty_callee_list_returns_empty(self):
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute"]})
        assert rule.find_prohibited_callees([]) == []


# ── needs_* predicates ────────────────────────────────────────────────────────

class TestPredicates:
    def test_needs_call_graph_check_when_prohibited(self):
        rule = ContractRule.from_expr({"prohibited_patterns": ["execute"]})
        assert rule.needs_call_graph_check() is True

    def test_needs_call_graph_check_when_required_callee(self):
        rule = ContractRule.from_expr({"required_callee": "read_secrets"})
        assert rule.needs_call_graph_check() is True

    def test_no_call_graph_check_when_empty(self):
        rule = ContractRule.from_expr({})
        assert rule.needs_call_graph_check() is False

    def test_needs_metadata_check_for_docstring(self):
        rule = ContractRule.from_expr({"missing_metadata": ["docstring"]})
        assert rule.needs_metadata_check() is True

    def test_no_metadata_check_when_empty(self):
        rule = ContractRule.from_expr({})
        assert rule.needs_metadata_check() is False

    def test_no_metadata_check_for_other_fields(self):
        rule = ContractRule.from_expr({"missing_metadata": ["summary"]})
        assert rule.needs_metadata_check() is False
