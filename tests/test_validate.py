"""
Tests for src/validate.py — validate_proposed_code conformance checker.

All tests use a parser stub and DB stub — no tree-sitter or database needed.
"""
import asyncio
import pytest
from src.validate import (
    Deviation,
    ValidationResult,
    validate_proposed_code,
    _check_naming,
    _check_async,
    _check_sequential_awaits_in_proposed,
    _check_db_in_loop,
)


def run(coro):
    return asyncio.run(coro)


# ── Stubs ──────────────────────────────────────────────────────────────────────

class _DB:
    def __init__(self, file_nodes=None, contracts=None):
        self._file_nodes = file_nodes or []
        self._contracts = contracts or []

    async def get_nodes_by_file(self, file_path, project_id=None):
        return self._file_nodes

    async def list_contracts(self, project_id=None):
        return self._contracts


def _node(name: str, is_async: int = 0, signature: str = "") -> dict:
    sig = signature or (f"async def {name}():" if is_async else f"def {name}():")
    return {"id": f"src.mod.{name}", "name": name, "is_async": is_async,
            "signature": sig, "file": "src/mod.py"}


def _contract(title: str, project_ids=None, status="active") -> dict:
    return {"id": "c1", "title": title, "status": status,
            "project_ids": project_ids or ["proj"], "function_ids": []}


# ── _check_naming ──────────────────────────────────────────────────────────────

class TestCheckNaming:
    def test_no_existing_returns_no_deviation(self):
        proposed = [{"name": "fetch_user", "is_async": False, "body": ""}]
        assert _check_naming(proposed, []) is None

    def test_matching_dominant_verb_no_deviation(self):
        existing = [_node("get_user"), _node("get_project"), _node("get_node"),
                    _node("get_edge"), _node("get_file")]
        proposed = [{"name": "get_session", "is_async": False, "body": ""}]
        assert _check_naming(proposed, existing) is None

    def test_non_matching_verb_returns_deviation(self):
        existing = [_node("get_user"), _node("get_project"), _node("get_node"),
                    _node("get_edge"), _node("get_file")]
        proposed = [{"name": "fetch_session", "is_async": False, "body": ""}]
        dev = _check_naming(proposed, existing)
        assert dev is not None
        assert dev.check == "naming"
        assert "fetch_session" in dev.message
        assert "get" in dev.message

    def test_no_dominant_existing_pattern_no_deviation(self):
        existing = [_node("get_user"), _node("create_project"), _node("delete_node"),
                    _node("list_edges"), _node("check_health")]
        proposed = [{"name": "fetch_session", "is_async": False, "body": ""}]
        assert _check_naming(proposed, existing) is None

    def test_example_field_contains_existing_names(self):
        existing = [_node("get_user"), _node("get_project"), _node("get_node"),
                    _node("get_edge"), _node("get_file")]
        proposed = [{"name": "load_session", "is_async": False, "body": ""}]
        dev = _check_naming(proposed, existing)
        assert dev is not None
        assert "get_user" in dev.example or "get_project" in dev.example

    def test_class_method_name_stripped_for_matching(self):
        existing = [_node("DB.get_user"), _node("DB.get_project"), _node("DB.get_node"),
                    _node("DB.get_edge"), _node("DB.get_file")]
        proposed = [{"name": "DB.fetch_session", "is_async": False, "body": ""}]
        dev = _check_naming(proposed, existing)
        assert dev is not None

    def test_matching_function_among_mixed_proposed_no_deviation(self):
        existing = [_node("get_user"), _node("get_project"), _node("get_node"),
                    _node("get_edge"), _node("get_file")]
        proposed = [{"name": "get_session", "is_async": False, "body": ""},
                    {"name": "get_token", "is_async": False, "body": ""}]
        assert _check_naming(proposed, existing) is None


# ── _check_async ───────────────────────────────────────────────────────────────

class TestCheckAsync:
    def test_async_first_module_sync_proposed_is_high_severity(self):
        existing = [_node(f"fn{i}", is_async=1) for i in range(8)]
        proposed = [{"name": "sync_fn", "is_async": False, "body": ""}]
        dev = _check_async(proposed, existing)
        assert dev is not None
        assert dev.severity == "high"
        assert "async" in dev.message.lower()

    def test_sync_first_module_async_proposed_is_medium_severity(self):
        existing = [_node(f"fn{i}", is_async=0) for i in range(7)]
        proposed = [{"name": "async_fn", "is_async": True, "body": ""}]
        dev = _check_async(proposed, existing)
        assert dev is not None
        assert dev.severity == "medium"

    def test_matching_async_convention_no_deviation(self):
        existing = [_node(f"fn{i}", is_async=1) for i in range(6)]
        proposed = [{"name": "new_fn", "is_async": True, "body": ""}]
        assert _check_async(proposed, existing) is None

    def test_mixed_module_no_deviation(self):
        existing = [_node(f"fn{i}", is_async=i % 2) for i in range(6)]
        proposed = [{"name": "sync_fn", "is_async": False, "body": ""}]
        assert _check_async(proposed, existing) is None

    def test_no_existing_no_deviation(self):
        proposed = [{"name": "fn", "is_async": False, "body": ""}]
        assert _check_async(proposed, []) is None

    def test_single_async_in_async_first_module_ok(self):
        existing = [_node(f"fn{i}", is_async=1) for i in range(5)]
        proposed = [{"name": "async_helper", "is_async": True, "body": ""}]
        assert _check_async(proposed, existing) is None


# ── _check_sequential_awaits_in_proposed ──────────────────────────────────────

class TestSequentialAwaitsCheck:
    def test_sequential_awaits_fires(self):
        body = (
            "async def fn():\n"
            "    a = await get_user()\n"
            "    b = await get_project()\n"
            "    return a, b\n"
        )
        proposed = [{"name": "fn", "is_async": True, "body": body}]
        dev = _check_sequential_awaits_in_proposed(proposed)
        assert dev is not None
        assert dev.check == "sequential_awaits"
        assert dev.severity == "high"

    def test_gather_already_present_no_deviation(self):
        body = (
            "async def fn():\n"
            "    a, b = await asyncio.gather(get_user(), get_project())\n"
            "    return a, b\n"
        )
        proposed = [{"name": "fn", "is_async": True, "body": body}]
        assert _check_sequential_awaits_in_proposed(proposed) is None

    def test_single_await_no_deviation(self):
        body = "async def fn():\n    a = await get_user()\n    return a\n"
        proposed = [{"name": "fn", "is_async": True, "body": body}]
        assert _check_sequential_awaits_in_proposed(proposed) is None

    def test_sync_function_skipped(self):
        body = "def fn():\n    a = get_user()\n    b = get_project()\n    return a, b\n"
        proposed = [{"name": "fn", "is_async": False, "body": body}]
        assert _check_sequential_awaits_in_proposed(proposed) is None

    def test_dependent_awaits_not_flagged(self):
        body = (
            "async def fn():\n"
            "    user = await get_user()\n"
            "    project = await get_project(user.id)\n"
            "    return project\n"
        )
        proposed = [{"name": "fn", "is_async": True, "body": body}]
        assert _check_sequential_awaits_in_proposed(proposed) is None


# ── _check_db_in_loop ──────────────────────────────────────────────────────────

class TestDbInLoopCheck:
    def test_db_access_in_loop_fires(self):
        body = (
            "async def load_all(ids):\n"
            "    results = []\n"
            "    for id in ids:\n"
            "        r = await _db.execute('SELECT * FROM nodes WHERE id=$1', (id,))\n"
            "        results.append(r)\n"
        )
        proposed = [{"name": "load_all", "is_async": True, "body": body}]
        dev = _check_db_in_loop(proposed)
        assert dev is not None
        assert dev.check == "n_plus_one"
        assert dev.severity == "high"

    def test_clean_loop_no_db_no_deviation(self):
        body = (
            "def process(items):\n"
            "    results = []\n"
            "    for item in items:\n"
            "        results.append(item * 2)\n"
            "    return results\n"
        )
        proposed = [{"name": "process", "is_async": False, "body": body}]
        assert _check_db_in_loop(proposed) is None

    def test_db_outside_loop_no_deviation(self):
        body = (
            "async def load(ids):\n"
            "    r = await _db.execute('SELECT * FROM nodes WHERE id=ANY($1)', (ids,))\n"
            "    return r\n"
        )
        proposed = [{"name": "load", "is_async": True, "body": body}]
        assert _check_db_in_loop(proposed) is None

    def test_conn_execute_in_loop_fires(self):
        body = (
            "async def sync_all(items):\n"
            "    for item in items:\n"
            "        await conn.execute('INSERT INTO t VALUES ($1)', (item,))\n"
        )
        proposed = [{"name": "sync_all", "is_async": True, "body": body}]
        dev = _check_db_in_loop(proposed)
        assert dev is not None


# ── ValidationResult ───────────────────────────────────────────────────────────

class TestValidationResult:
    def test_to_dict_shape(self):
        r = ValidationResult(
            conformance_score=0.75,
            proposed_functions=["get_user"],
            module="src.storage",
            deviations=[Deviation(check="naming", severity="medium",
                                  message="msg", example="ex")],
            active_constraints=["Contract: All via execute"],
        )
        d = r.to_dict()
        assert d["conformance_score"] == 0.75
        assert d["proposed_functions"] == ["get_user"]
        assert d["module"] == "src.storage"
        assert len(d["deviations"]) == 1
        assert d["deviations"][0]["check"] == "naming"
        assert d["active_constraints"] == ["Contract: All via execute"]

    def test_perfect_score_no_deviations(self):
        r = ValidationResult(1.0, ["fn"], "src.mod", [], [])
        d = r.to_dict()
        assert d["conformance_score"] == 1.0
        assert d["deviations"] == []


# ── validate_proposed_code integration ────────────────────────────────────────

class TestValidateProposedCode:
    def test_clean_code_returns_high_score(self):
        existing = [_node(f"get_item{i}", is_async=1) for i in range(5)]
        db = _DB(file_nodes=existing)
        code = "async def get_session(user_id: str):\n    pass\n"
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        assert result.conformance_score >= 0.75
        assert isinstance(result, ValidationResult)

    def test_naming_violation_lowers_score(self):
        existing = [_node(f"get_item{i}", is_async=1) for i in range(5)]
        db = _DB(file_nodes=existing)
        code = "async def fetch_session():\n    pass\n"
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        assert result.conformance_score < 1.0
        checks = [d.check for d in result.deviations]
        assert "naming" in checks

    def test_async_violation_lowers_score(self):
        existing = [_node(f"fn{i}", is_async=1) for i in range(6)]
        db = _DB(file_nodes=existing)
        code = "def sync_helper():\n    pass\n"
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        checks = [d.check for d in result.deviations]
        assert "async" in checks

    def test_sequential_awaits_flagged(self):
        db = _DB()
        code = (
            "async def load():\n"
            "    a = await get_x()\n"
            "    b = await get_y()\n"
            "    return a, b\n"
        )
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        checks = [d.check for d in result.deviations]
        assert "sequential_awaits" in checks

    def test_db_in_loop_flagged(self):
        db = _DB()
        code = (
            "async def load_all(ids):\n"
            "    for id in ids:\n"
            "        await _db.execute('SELECT 1', (id,))\n"
        )
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        checks = [d.check for d in result.deviations]
        assert "n_plus_one" in checks

    def test_active_contract_surfaced_as_constraint(self):
        db = _DB(contracts=[_contract("All DB via execute", project_ids=["proj"])])
        code = "async def fn():\n    pass\n"
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        assert any("All DB via execute" in c for c in result.active_constraints)

    def test_proposed_functions_listed_in_result(self):
        db = _DB()
        code = "def get_user():\n    pass\ndef create_user():\n    pass\n"
        result = run(validate_proposed_code(code, "src/mod.py", "proj", db))
        assert "get_user" in result.proposed_functions
        assert "create_user" in result.proposed_functions

    def test_empty_code_returns_valid_result(self):
        db = _DB()
        result = run(validate_proposed_code("", "src/mod.py", "proj", db))
        assert isinstance(result, ValidationResult)
        assert result.proposed_functions == []

    def test_score_decreases_with_each_deviation(self):
        existing = [_node(f"get_item{i}", is_async=1) for i in range(5)]
        db = _DB(file_nodes=existing)
        # naming violation (no async violation since no existing async context issue)
        code_one = "async def fetch_session():\n    pass\n"
        result_one = run(validate_proposed_code(code_one, "src/mod.py", "proj", db))
        # naming + async violation
        code_two = "def fetch_session():\n    pass\n"
        result_two = run(validate_proposed_code(code_two, "src/mod.py", "proj", db))
        assert result_two.conformance_score <= result_one.conformance_score
