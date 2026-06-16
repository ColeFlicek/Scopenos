"""
Tests for the performance detection layer (src/performance.py).

Public interfaces under test:
  - detect_correlated_join_aggregate(sql) → str | None
  - detect_n_plus_one(nodes_by_id, callee_map, db_sink_ids) → list[tuple]
  - _score_n_plus_one(node, callee_names, schema_objects, embeddings_by_name)
  - Finding.to_dict()

No database, no async I/O except where detect_n_plus_one is called
(it is async but needs no DB — tested with in-memory dicts).
"""
import asyncio
import pytest
from src.performance import (
    Finding,
    detect_correlated_join_aggregate,
    detect_n_plus_one,
    _score_n_plus_one,
)
from src.schema_objects import SchemaObject


# ── Helpers ───────────────────────────────────────────────────────────────────

def _node(node_id: str, body: str = "", name: str = "") -> dict:
    return {
        "id": node_id,
        "name": name or node_id.split(".")[-1],
        "file": "src/mod.py",
        "body": body,
    }


def _schema_obj(name: str, cardinality: str) -> SchemaObject:
    return SchemaObject(
        name=name,
        source="db_table",
        project_id="test",
        cardinality=cardinality,
        description=f"Table {name}",
    )


def run(coro):
    return asyncio.run(coro)


# ── SQL cross-join detector ───────────────────────────────────────────────────

CROSS_JOIN_SQL = """
    SELECT p.id,
           COUNT(DISTINCT n.id) AS node_count,
           COUNT(DISTINCT e.id) AS edge_count
    FROM projects p
    LEFT JOIN nodes n ON n.project_id = p.id
    LEFT JOIN edges e ON e.project_id = p.id
    GROUP BY p.id
"""

CORRELATED_SQL = """
    SELECT p.id,
           (SELECT COUNT(*) FROM nodes n WHERE n.project_id = p.id) AS node_count,
           (SELECT COUNT(*) FROM edges e WHERE e.project_id = p.id) AS edge_count
    FROM projects p
"""

SINGLE_JOIN_SQL = """
    SELECT p.id, COUNT(DISTINCT n.id)
    FROM projects p
    LEFT JOIN nodes n ON n.project_id = p.id
    GROUP BY p.id
"""


class TestDetectCorrelatedJoinAggregate:
    def test_two_joins_with_shared_key_flagged(self):
        """The original list_projects bug: nodes × edges cross-product."""
        result = detect_correlated_join_aggregate(CROSS_JOIN_SQL)
        assert result is not None

    def test_correlated_subqueries_not_flagged(self):
        """Correlated subqueries are the correct pattern — must not fire."""
        result = detect_correlated_join_aggregate(CORRELATED_SQL)
        assert result is None

    def test_single_join_not_flagged(self):
        """One JOIN with GROUP BY + COUNT is fine — no cross-product possible."""
        result = detect_correlated_join_aggregate(SINGLE_JOIN_SQL)
        assert result is None

    def test_two_joins_no_count_distinct_not_flagged(self):
        sql = """
            SELECT p.id, p.name
            FROM projects p
            LEFT JOIN nodes n ON n.project_id = p.id
            LEFT JOIN edges e ON e.project_id = p.id
            GROUP BY p.id
        """
        result = detect_correlated_join_aggregate(sql)
        assert result is None

    def test_two_joins_no_group_by_not_flagged(self):
        sql = """
            SELECT p.id, COUNT(DISTINCT n.id)
            FROM projects p
            LEFT JOIN nodes n ON n.project_id = p.id
            LEFT JOIN edges e ON e.project_id = p.id
        """
        result = detect_correlated_join_aggregate(sql)
        assert result is None

    def test_flagged_result_mentions_tables(self):
        result = detect_correlated_join_aggregate(CROSS_JOIN_SQL)
        # Should name the joined tables so the developer knows what to fix
        assert "NODES" in result.upper() or "EDGES" in result.upper()

    def test_case_insensitive(self):
        lower = CROSS_JOIN_SQL.lower()
        result = detect_correlated_join_aggregate(lower)
        assert result is not None

    def test_empty_string_not_flagged(self):
        assert detect_correlated_join_aggregate("") is None

    def test_plain_select_not_flagged(self):
        assert detect_correlated_join_aggregate("SELECT id FROM projects") is None


# ── N+1 detector ─────────────────────────────────────────────────────────────

class TestDetectNPlusOne:
    def test_loop_calling_db_sink_is_flagged(self):
        """A function with a for-loop that directly calls a DB function."""
        nodes = {
            "src.mod.process_all": _node(
                "src.mod.process_all",
                body="for item in items:\n    result = fetch_item(item)"
            ),
            "src.mod.fetch_item": _node(
                "src.mod.fetch_item",
                body="async with self._db.execute('SELECT ...')"
            ),
        }
        callee_map = {"src.mod.process_all": ["src.mod.fetch_item"]}
        db_sinks = {"src.mod.fetch_item"}

        findings = run(detect_n_plus_one(nodes, callee_map, db_sinks))
        assert len(findings) == 1
        assert findings[0][0] == "src.mod.process_all"

    def test_loop_without_db_callee_not_flagged(self):
        """A pure loop with no DB calls is not an N+1."""
        nodes = {
            "src.mod.process_all": _node(
                "src.mod.process_all",
                body="for item in items:\n    compute(item)"
            ),
            "src.mod.compute": _node("src.mod.compute", body="return item * 2"),
        }
        callee_map = {"src.mod.process_all": ["src.mod.compute"]}
        db_sinks = set()

        findings = run(detect_n_plus_one(nodes, callee_map, db_sinks))
        assert findings == []

    def test_db_sink_without_loop_not_flagged(self):
        """A DB-calling function with no loop is not N+1."""
        nodes = {
            "src.mod.fetch_all": _node(
                "src.mod.fetch_all",
                body="rows = await conn.fetch('SELECT * FROM nodes')"
            ),
        }
        callee_map = {}
        db_sinks = {"src.mod.fetch_all"}

        findings = run(detect_n_plus_one(nodes, callee_map, db_sinks))
        assert findings == []

    def test_transitive_db_callee_flagged(self):
        """Loop → helper → DB sink (depth 2) is still flagged."""
        nodes = {
            "src.mod.outer": _node(
                "src.mod.outer",
                body="for x in items:\n    helper(x)"
            ),
            "src.mod.helper": _node("src.mod.helper", body="call_db()"),
            "src.mod.call_db": _node(
                "src.mod.call_db",
                body="await conn.fetch('SELECT ...')"
            ),
        }
        callee_map = {
            "src.mod.outer": ["src.mod.helper"],
            "src.mod.helper": ["src.mod.call_db"],
        }
        db_sinks = {"src.mod.call_db"}

        findings = run(detect_n_plus_one(nodes, callee_map, db_sinks))
        assert len(findings) == 1
        assert findings[0][0] == "src.mod.outer"

    def test_empty_graph_returns_no_findings(self):
        findings = run(detect_n_plus_one({}, {}, set()))
        assert findings == []


# ── N+1 severity scoring ─────────────────────────────────────────────────────

class TestScoreNPlusOne:
    def test_executemany_body_is_low_severity(self):
        """Batch writes via executemany are intentional — must not be high severity."""
        node = _node("fn", body="await conn.executemany('INSERT ...', rows)")
        severity, detail = _score_n_plus_one(node, [], [], {})
        assert severity == "low"
        assert "executemany" in detail.lower() or "batch" in detail.lower()

    def test_high_cardinality_object_is_high_severity(self):
        node = _node("fn", body="for n in get_nodes():")
        schema = [_schema_obj("nodes", "HIGH")]
        severity, _ = _score_n_plus_one(node, ["get_nodes"], schema, {})
        assert severity == "high"

    def test_low_cardinality_object_is_low_severity(self):
        node = _node("fn", body="for p in get_projects():")
        schema = [_schema_obj("projects", "LOW")]
        severity, _ = _score_n_plus_one(node, ["get_projects"], schema, {})
        assert severity == "low"

    def test_no_schema_objects_defaults_to_medium(self):
        node = _node("fn", body="for x in items:")
        severity, _ = _score_n_plus_one(node, ["some_callee"], [], {})
        assert severity == "medium"

    def test_unbounded_cardinality_is_high(self):
        node = _node("fn", body="for e in scan_embeddings():")
        schema = [_schema_obj("embeddings", "UNBOUNDED")]
        severity, _ = _score_n_plus_one(node, ["scan_embeddings"], schema, {})
        assert severity == "high"

    def test_executemany_overrides_high_cardinality(self):
        """Batch write pattern beats cardinality — executemany is always low."""
        node = _node("fn", body="await conn.executemany('INSERT ...', data)")
        schema = [_schema_obj("nodes", "HIGH")]
        severity, _ = _score_n_plus_one(node, ["upsert_nodes"], schema, {})
        assert severity == "low"


# ── Finding serialisation ─────────────────────────────────────────────────────

class TestFindingToDict:
    def test_new_finding_status_is_new(self):
        f = Finding(
            function_id="src.mod.fn",
            function_name="fn",
            file="src/mod.py",
            pattern="n_plus_one",
            severity="high",
            detail="loop calls DB",
        )
        d = f.to_dict()
        assert d["status"] == "new"
        assert "acknowledged_reason" not in d

    def test_suppressed_finding_includes_reason(self):
        f = Finding(
            function_id="src.mod.fn",
            function_name="fn",
            file="src/mod.py",
            pattern="n_plus_one",
            severity="high",
            detail="loop calls DB",
            suppressed=True,
            suppression_reason="intentional batch sync",
        )
        d = f.to_dict()
        assert d["status"] == "acknowledged"
        assert d["acknowledged_reason"] == "intentional batch sync"

    def test_required_fields_present(self):
        f = Finding(
            function_id="x", function_name="fn", file="f.py",
            pattern="sql_cross_join", severity="medium", detail="detail"
        )
        d = f.to_dict()
        for key in ("function", "file", "pattern", "severity", "detail", "status"):
            assert key in d
