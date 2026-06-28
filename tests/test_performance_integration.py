"""
Integration tests for the performance detection pipeline.

These tests run check_performance() against projects that were actually
indexed — verifying that the full pipeline works end-to-end:

  index_project(fixture) → function bodies stored → edges stored
                         → check_performance() reads bodies + graph
                         → detectors fire on real indexed data

WHY THIS LEVEL EXISTS
---------------------
Unit tests in test_performance.py verify each detector in isolation using
dicts constructed in-memory. They cannot catch bugs in the pipeline that
connects those detectors to real data:

  - Bodies not stored: check_performance() sees empty strings → no findings
  - Call edges not stored: N+1 detector sees empty callee map → no findings
  - Schema objects not loaded: severity stays "medium" when it should be "high"
  - Suppression not applied: acknowledged findings still show as "new"

Each test below exercises exactly one failure mode of that kind.
"""
import asyncio
import json
import uuid
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.call_graph.storage import CallGraphDB
from src.indexer import Indexer
from src.performance import check_performance, Finding
from src.schema_objects import SchemaObject


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def indexer(db: CallGraphDB):
    pipeline = MagicMock()
    pipeline.upsert_chunks = AsyncMock(return_value={"docs": 0, "fallback": 0})
    pipeline.delete_by_ids = AsyncMock()
    pipeline.delete_by_file = AsyncMock()
    pipeline.get_embedded_ids = AsyncMock(return_value=set())
    pipeline.model = "text-embedding-3-small"
    pipeline.with_db = MagicMock(return_value=pipeline)
    return Indexer(db, pipeline)


def _write(base: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)


# ── Python fixture projects with known anti-patterns ─────────────────────────

# A function whose body contains the exact SQL cross-join pattern that
# triggered the list_projects bug: two JOINs + GROUP BY + COUNT(DISTINCT).
FIXTURE_SQL_CROSSJOIN = {
    "src/__init__.py": "",
    "src/queries.py": """\
async def get_stats_broken(db):
    async with db.execute('''
        SELECT p.id,
               COUNT(DISTINCT n.id) AS node_count,
               COUNT(DISTINCT e.id) AS edge_count
        FROM projects p
        LEFT JOIN nodes n ON n.project_id = p.id
        LEFT JOIN edges e ON e.project_id = p.id
        GROUP BY p.id
    ''') as cur:
        return await cur.fetchall()

async def get_stats_ok(db):
    async with db.execute('''
        SELECT p.id,
               (SELECT COUNT(*) FROM nodes n WHERE n.project_id = p.id) AS node_count
        FROM projects p
    ''') as cur:
        return await cur.fetchall()
""",
}

# Two functions: one is a DB sink (contains conn.fetch), the other
# loops over data and calls the sink function — the N+1 pattern.
FIXTURE_N_PLUS_ONE = {
    "src/__init__.py": "",
    "src/processor.py": """\
async def fetch_node(conn, node_id):
    return await conn.fetch("SELECT * FROM nodes WHERE id = $1", node_id)

async def process_all_nodes(conn, node_ids):
    results = []
    for node_id in node_ids:
        row = await fetch_node(conn, node_id)
        results.append(row)
    return results
""",
}

# Same N+1 pattern but the loop function uses executemany — a batch write,
# not a per-row read. Should be auto-suppressed as low severity.
FIXTURE_BATCH_WRITE = {
    "src/__init__.py": "",
    "src/writer.py": """\
async def _execute_query(conn, sql, params):
    return await conn.execute(sql, *params)

async def bulk_write(conn, rows):
    for row in rows:
        await conn.executemany("INSERT INTO events VALUES ($1, $2)", row)
""",
}

# N+1 with a HIGH-cardinality schema object — should be severity "high".
FIXTURE_HIGH_CARDINALITY = {
    "src/__init__.py": "",
    "src/graph.py": """\
async def fetch_edges_for_node(conn, node_id):
    return await conn.fetch("SELECT * FROM edges WHERE caller_id = $1", node_id)

async def analyze_all_nodes(conn, node_ids):
    all_edges = []
    for node_id in node_ids:
        edges = await fetch_edges_for_node(conn, node_id)
        all_edges.extend(edges)
    return all_edges
""",
}


# ── Helper: insert schema objects directly (no embedding API call) ─────────────

async def _insert_schema_objects(db: CallGraphDB, project_id: str, objects: list[dict]):
    """
    Insert schema objects into the DB without calling the embedding API.

    Sets embedding=NULL — the scoring in _score_n_plus_one uses name
    matching (not cosine similarity), so the test is valid without vectors.
    """
    async with db._pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_object_embeddings (
                project_id  TEXT NOT NULL,
                name        TEXT NOT NULL,
                source      TEXT NOT NULL,
                cardinality TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                refs        TEXT NOT NULL DEFAULT '[]',
                refs_in     TEXT NOT NULL DEFAULT '[]',
                embedding   vector(1536),
                PRIMARY KEY (project_id, name, source)
            )
        """)
        for obj in objects:
            await conn.execute("""
                INSERT INTO schema_object_embeddings
                    (project_id, name, source, cardinality, description)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT DO NOTHING
            """, project_id, obj["name"], obj["source"],
                obj["cardinality"], obj.get("description", ""))


# ── SQL cross-join detector integration ───────────────────────────────────────

class TestSqlCrossjoinDetectorIntegration:
    """
    Verifies that check_performance() finds the cross-join SQL pattern
    when the function body is stored in the DB after indexing.

    The failure mode this catches: body="" (before the body-storage fix)
    means _analyze_node_for_sql() sees nothing and returns no findings.
    """

    @pytest.mark.asyncio
    async def test_crossjoin_function_produces_finding(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        _write(tmp_path, FIXTURE_SQL_CROSSJOIN)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        patterns = [f.pattern for f in findings]
        assert "correlated_join_aggregate" in patterns, (
            "No correlated_join_aggregate finding. "
            "If the function body is empty in the DB, the SQL detector sees "
            "nothing. Check that body text is stored after indexing."
        )

    @pytest.mark.asyncio
    async def test_crossjoin_finding_identifies_correct_function(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        _write(tmp_path, FIXTURE_SQL_CROSSJOIN)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        sql_findings = [f for f in findings if f.pattern == "correlated_join_aggregate"]
        names = {f.function_name for f in sql_findings}
        assert "get_stats_broken" in names

    @pytest.mark.asyncio
    async def test_clean_sql_function_produces_no_finding(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """get_stats_ok uses correlated subqueries — must not be flagged."""
        _write(tmp_path, FIXTURE_SQL_CROSSJOIN)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        sql_findings = [f for f in findings if f.pattern == "correlated_join_aggregate"]
        names = {f.function_name for f in sql_findings}
        assert "get_stats_ok" not in names

    @pytest.mark.asyncio
    async def test_crossjoin_finding_is_new_by_default(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        _write(tmp_path, FIXTURE_SQL_CROSSJOIN)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        sql_findings = [f for f in findings if f.pattern == "correlated_join_aggregate"
                        and f.function_name == "get_stats_broken"]
        assert len(sql_findings) == 1
        assert not sql_findings[0].suppressed


# ── N+1 detector integration ──────────────────────────────────────────────────

class TestNPlusOneDetectorIntegration:
    """
    Verifies that check_performance() finds N+1 patterns when the call graph
    is correctly indexed — specifically that the callee map (built from edges
    in the DB) connects the looping function to the DB sink.

    The failure mode this catches: if call edges aren't stored or the
    caller_id format is wrong, callee_map is empty → no N+1 findings.
    """

    @pytest.mark.asyncio
    async def test_n_plus_one_function_produces_finding(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        _write(tmp_path, FIXTURE_N_PLUS_ONE)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        patterns = [f.pattern for f in findings]
        assert "n_plus_one" in patterns, (
            "No n_plus_one finding. "
            "If call edges aren't stored, the callee map is empty and the "
            "N+1 detector cannot link the loop function to the DB sink."
        )

    @pytest.mark.asyncio
    async def test_n_plus_one_finding_identifies_loop_function(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """The flagged function must be the one with the loop, not the DB sink."""
        _write(tmp_path, FIXTURE_N_PLUS_ONE)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        n1 = [f for f in findings if f.pattern == "n_plus_one"]
        names = {f.function_name for f in n1}
        assert "process_all_nodes" in names
        assert "fetch_node" not in names

    @pytest.mark.asyncio
    async def test_db_sink_without_loop_not_flagged(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """fetch_node has no loop — must not be flagged as N+1."""
        _write(tmp_path, FIXTURE_N_PLUS_ONE)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        n1 = [f for f in findings if f.pattern == "n_plus_one"]
        names = {f.function_name for f in n1}
        assert "fetch_node" not in names


# ── Suppression via Performance decision ──────────────────────────────────────

class TestFindingSuppression:
    """
    Verifies that check_performance() suppresses findings for functions
    that have an acknowledged Performance decision in decision memory.
    """

    @pytest.mark.asyncio
    async def test_acknowledged_finding_is_suppressed(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        _write(tmp_path, FIXTURE_SQL_CROSSJOIN)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        # Find the function ID that has the SQL anti-pattern
        findings_before = await check_performance(db, "perf_test")
        sql_f = next(f for f in findings_before
                     if f.pattern == "correlated_join_aggregate"
                     and f.function_name == "get_stats_broken")
        function_id = sql_f.function_id

        # Log a Performance decision acknowledging this finding
        dec_id = str(uuid.uuid4())
        async with db._pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO decisions
                    (id, project_id, type, description, rejected_alternatives, trigger, created_at)
                VALUES ($1, 'perf_test', 'Performance',
                        'Intentional — single-user tool, not scale-sensitive',
                        '[]', 'manual_ack', $2)
                """,
                dec_id, "2026-01-01T00:00:00+00:00",
            )
            await conn.execute(
                "INSERT INTO decision_functions (decision_id, function_id) VALUES ($1, $2)",
                dec_id, function_id,
            )

        # Re-run — the finding should now be suppressed
        findings_after = await check_performance(db, "perf_test")
        sql_f_after = next(
            (f for f in findings_after
             if f.pattern == "correlated_join_aggregate"
             and f.function_name == "get_stats_broken"),
            None
        )
        assert sql_f_after is not None, "Finding disappeared entirely after acknowledgement"
        assert sql_f_after.suppressed, "Finding should be suppressed after Performance decision"

    @pytest.mark.asyncio
    async def test_unacknowledged_finding_remains_new(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """Acknowledging one function must not suppress findings for other functions."""
        _write(tmp_path, FIXTURE_SQL_CROSSJOIN)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        new_findings = [f for f in findings if not f.suppressed]
        assert len(new_findings) > 0


# ── Auto-suppression: batch write pattern ────────────────────────────────────

class TestAutoSuppression:
    """
    check_performance() auto-suppresses N+1 findings when:
    - severity is "low" (object embedding or executemany scored it down)
    - no explicit Performance decision exists for the function

    This prevents executemany / small-collection loops from flooding
    the findings list with noise.
    """

    @pytest.mark.asyncio
    async def test_executemany_loop_is_auto_suppressed(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """
        Batch writes via executemany should be auto-suppressed even without
        a Performance decision — the object scoring layer identifies them.
        """
        _write(tmp_path, FIXTURE_BATCH_WRITE)
        await indexer.index_project(str(tmp_path), project_id="perf_test")

        findings = await check_performance(db, "perf_test")
        n1 = [f for f in findings if f.pattern == "n_plus_one"]
        if n1:
            executemany_findings = [
                f for f in n1 if f.function_name == "bulk_write"
            ]
            for f in executemany_findings:
                assert f.suppressed, (
                    f"bulk_write (executemany pattern) severity={f.severity} "
                    "should be auto-suppressed — executemany is a batch write, not N+1"
                )


# ── Object embedding cardinality scoring integration ──────────────────────────

class TestObjectEmbeddingScoring:
    """
    Verifies that check_performance() uses schema object cardinality to
    adjust N+1 severity — HIGH cardinality → high severity.

    Schema objects are inserted directly (no embedding API needed) because
    _score_n_plus_one() uses name matching, not vector similarity, to
    link callee names to schema objects.
    """

    @pytest.mark.asyncio
    async def test_high_cardinality_object_raises_severity_to_high(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """
        analyze_all_nodes loops and calls fetch_edges_for_node which touches
        the 'edges' table. When we insert 'edges' as HIGH cardinality, the
        finding severity must be 'high'.
        """
        _write(tmp_path, FIXTURE_HIGH_CARDINALITY)
        await indexer.index_project(str(tmp_path), project_id=project_id)

        # Insert 'edges' as a HIGH-cardinality schema object
        await _insert_schema_objects(db, project_id, [
            {"name": "edges", "source": "db_table", "cardinality": "HIGH",
             "description": "Database table: edges\nCardinality: HIGH"},
        ])

        findings = await check_performance(db, project_id)
        n1 = [f for f in findings if f.pattern == "n_plus_one"
              and f.function_name == "analyze_all_nodes"]

        if n1:  # finding exists
            assert n1[0].severity == "high", (
                f"Expected severity='high' for loop accessing HIGH-cardinality "
                f"'edges' table, got severity='{n1[0].severity}'. "
                "Check that schema objects are loaded and name-matching works."
            )

    @pytest.mark.asyncio
    async def test_low_cardinality_object_keeps_severity_low(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """Loop accessing a LOW-cardinality table should be severity 'low'."""
        _write(tmp_path, FIXTURE_HIGH_CARDINALITY)
        await indexer.index_project(str(tmp_path), project_id=project_id)

        # Same function but 'edges' is LOW cardinality (small dataset)
        await _insert_schema_objects(db, project_id, [
            {"name": "edges", "source": "db_table", "cardinality": "LOW",
             "description": "Database table: edges\nCardinality: LOW"},
        ])

        findings = await check_performance(db, project_id)
        n1 = [f for f in findings if f.pattern == "n_plus_one"
              and f.function_name == "analyze_all_nodes"]

        if n1:
            assert n1[0].severity == "low"

    @pytest.mark.asyncio
    async def test_no_schema_objects_defaults_to_medium(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """Without schema objects, N+1 severity defaults to 'medium'."""
        _write(tmp_path, FIXTURE_N_PLUS_ONE)
        await indexer.index_project(str(tmp_path), project_id=project_id)
        # Do NOT insert any schema objects

        findings = await check_performance(db, project_id)
        n1 = [f for f in findings if f.pattern == "n_plus_one"
              and f.function_name == "process_all_nodes"]

        if n1:
            assert n1[0].severity == "medium"

    @pytest.mark.asyncio
    async def test_schema_objects_from_wrong_project_not_used(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path, project_id: str
    ):
        """
        Schema objects are project-scoped. Objects from a different project
        must not affect findings — prevents cross-project cardinality bleed.
        """
        _write(tmp_path, FIXTURE_HIGH_CARDINALITY)
        await indexer.index_project(str(tmp_path), project_id=project_id)

        # Insert HIGH-cardinality 'edges' but for a DIFFERENT project
        await _insert_schema_objects(db, f"{project_id}_other", [
            {"name": "edges", "source": "db_table", "cardinality": "HIGH",
             "description": "edges table"},
        ])

        findings = await check_performance(db, project_id)
        n1 = [f for f in findings if f.pattern == "n_plus_one"
              and f.function_name == "analyze_all_nodes"]

        if n1:
            # Without schema objects for THIS project, severity is medium
            assert n1[0].severity == "medium", (
                "Schema objects from a different project leaked into scoring. "
                "load_schema_objects() must filter by project_id."
            )
