"""
Integration tests for the full index → query pipeline.

These tests run Indexer.index_project() on small Python fixture projects
created in tmp_path, then query the resulting call graph through the
real storage layer. They exercise the complete pipeline in one shot:

  source files → tree-sitter parser → SCIP indexer (if available)
               → upsert_nodes / upsert_edges
               → get_callers / get_callees / get_impact_radius

WHY THIS LEVEL EXISTS
---------------------
Unit tests verify individual functions in isolation. They cannot catch bugs
where two components produce inconsistent data formats. The SCIP caller_id
bug is the canonical example: _parse_binary() emitted mangled caller_ids,
upsert_edges() stored them, and get_callers() JOINed on n.id — all three
functions worked correctly in isolation, but the pipeline produced no results.

Only a test that runs index→query end-to-end can catch that class of bug.

SETUP
-----
Requires a Postgres DB (DATABASE_URL or default phronosis_test). Set via:
    DATABASE_URL=postgresql://phronosis:phronosis@localhost/phronosis_test pytest

The `db` fixture is in the root conftest.py and truncates all tables before
each test for isolation. Embeddings are mocked — no API key required.
"""
import shutil
import pytest
import pytest_asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from src.call_graph.storage import CallGraphDB
from src.indexer import Indexer


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def indexer(db: CallGraphDB):
    """Indexer with real DB and mocked embedding pipeline (no API key needed)."""
    pipeline = MagicMock()
    pipeline.upsert_chunks = AsyncMock(return_value={"docs": 0, "fallback": 0})
    pipeline.delete_by_ids = AsyncMock()
    pipeline.delete_by_file = AsyncMock()
    # get_embedded_ids is called by _verify_coverage — return empty set (no embeddings)
    pipeline.get_embedded_ids = AsyncMock(return_value=set())
    pipeline.model = "text-embedding-3-small"
    return Indexer(db, pipeline)


def _write_project(base: Path, files: dict[str, str]) -> None:
    """Write a dict of relative_path→content into base directory."""
    for rel, content in files.items():
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content)


SCIP_AVAILABLE = shutil.which("scip-python") is not None
needs_scip = pytest.mark.skipif(
    not SCIP_AVAILABLE,
    reason="scip-python not installed — SCIP integration tests skipped",
)


# ── Fixture projects ──────────────────────────────────────────────────────────

#  calculator.py defines two functions.
#  app.py imports and calls both — establishing a real call graph edge.
FIXTURE_SIMPLE = {
    "src/__init__.py": "",
    "src/calculator.py": """\
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
""",
    "src/app.py": """\
from .calculator import add, multiply

def compute(x, y):
    result = add(x, y)
    return multiply(result, 2)

def main():
    value = compute(3, 4)
    return value
""",
}

#  A project where one function is deeply nested in the call chain.
#  outer → middle → inner → (implicitly ends)
FIXTURE_CHAIN = {
    "src/__init__.py": "",
    "src/chain.py": """\
def inner():
    return 42

def middle():
    return inner()

def outer():
    return middle()
""",
}


# ── Index → callers/callees ───────────────────────────────────────────────────

class TestCallGraphAfterIndex:
    """
    Core behavioral contract: after indexing a project, the call graph queries
    must return accurate results reflecting the actual source code structure.
    """

    @pytest.mark.asyncio
    async def test_indexed_project_appears_in_list_projects(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        projects = await db.list_projects()
        ids = [p["id"] for p in projects]
        assert "test_proj" in ids

    @pytest.mark.asyncio
    async def test_functions_are_discoverable_after_index(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        nodes = await db.get_all_nodes("test_proj")
        names = {n["name"] for n in nodes}
        assert "add" in names
        assert "multiply" in names
        assert "compute" in names
        assert "main" in names

    @pytest.mark.asyncio
    async def test_get_callees_returns_called_functions(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """compute() calls add() and multiply() — both must appear in get_callees."""
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        callees = await db.get_callees("compute", project_id="test_proj")
        callee_names = {c["name"] for c in callees}
        assert "add" in callee_names
        assert "multiply" in callee_names

    @pytest.mark.asyncio
    async def test_get_callers_returns_calling_functions(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """add() is called by compute() — compute must appear in get_callers(add)."""
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        callers = await db.get_callers("add", project_id="test_proj")
        caller_names = {c["name"] for c in callers}
        assert "compute" in caller_names

    @pytest.mark.asyncio
    async def test_get_callers_not_empty_after_index(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """
        Regression for the SCIP caller_id mismatch bug.

        If SCIP edges store mangled caller_ids (like 'src.app.scip-python_...'),
        the JOIN in get_callers() finds no matching node and returns []. This
        test would have caught that bug: after a real index, any called function
        must have at least one discoverable caller in the project.
        """
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        # add() is called by compute() — must not return empty
        callers = await db.get_callers("add", project_id="test_proj")
        assert len(callers) > 0, (
            "get_callers('add') returned empty after indexing. "
            "This likely means edge caller_ids don't match stored node IDs — "
            "the SCIP caller_id mismatch bug."
        )

    @pytest.mark.asyncio
    async def test_get_impact_radius_traverses_chain(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """outer→middle→inner: impact radius from inner should reach outer at depth 2."""
        _write_project(tmp_path, FIXTURE_CHAIN)
        await indexer.index_project(str(tmp_path), project_id="test_chain")

        radius = await db.get_impact_radius("inner", depth=2, project_id="test_chain")
        names = {r["name"] for r in radius}
        assert "middle" in names   # depth 1 caller
        assert "outer" in names    # depth 2 caller

    @pytest.mark.asyncio
    async def test_node_ids_use_dotted_module_format(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """
        Node IDs must follow the tree-sitter module.function format so that
        SCIP edges (which use the same format after the caller_id fix) resolve
        correctly in get_callers/get_callees JOINs.
        """
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        nodes = await db.get_all_nodes("test_proj")
        for node in nodes:
            if node["name"] == "add":
                assert "." in node["id"], f"Node ID '{node['id']}' should be dotted"
                assert "scip-python" not in node["id"], (
                    f"Node ID '{node['id']}' contains SCIP package hash — "
                    "tree-sitter IDs must not contain SCIP metadata."
                )

    @pytest.mark.asyncio
    async def test_re_index_does_not_duplicate_nodes(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """Indexing the same project twice must not double the node count."""
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")
        count_first = len(await db.get_all_nodes("test_proj"))

        await indexer.index_project(str(tmp_path), project_id="test_proj")
        count_second = len(await db.get_all_nodes("test_proj"))

        assert count_second == count_first


# ── SCIP-specific: external library stubs ─────────────────────────────────────

class TestScipExternalStubsAfterIndex:
    """
    When scip-python is installed, indexing should produce external library
    stub nodes visible via get_callees(). These tests are skipped if
    scip-python is not in PATH (e.g. local dev without the Docker image).
    """

    FIXTURE_WITH_IMPORT = {
        "src/__init__.py": "",
        "src/fetcher.py": """\
import os
import json

def read_config(path):
    with open(path) as f:
        return json.load(f)

def get_env(key):
    return os.environ.get(key, "")
""",
    }

    @needs_scip
    @pytest.mark.asyncio
    async def test_external_stubs_present_after_scip_index(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """After a SCIP index, external library calls must appear as stub nodes."""
        _write_project(tmp_path, self.FIXTURE_WITH_IMPORT)
        result = await indexer.index_project(str(tmp_path), project_id="test_scip")

        assert result.get("structural_layer") == "tree-sitter+scip", (
            f"Expected tree-sitter+scip structural layer, got {result.get('structural_layer')}. "
            "scip-python may be installed but produced no output."
        )
        assert result.get("external_nodes", 0) > 0, (
            "No external stub nodes after SCIP index. SCIP ran but produced no external symbols."
        )

    @needs_scip
    @pytest.mark.asyncio
    async def test_external_callees_discoverable_via_get_callees(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """
        The core SCIP value proposition: get_callees should surface external
        library symbols, not just internal project functions.

        This test would catch a regression where SCIP edges are stored but
        cannot be found via get_callees (e.g. callee_id format mismatch).
        """
        _write_project(tmp_path, self.FIXTURE_WITH_IMPORT)
        await indexer.index_project(str(tmp_path), project_id="test_scip")

        callees = await db.get_callees("get_env", project_id="test_scip")
        external = [c for c in callees if c.get("is_external")]
        assert len(external) > 0, (
            "get_callees('get_env') returned no external stubs after SCIP index. "
            "Expected os.environ.get or similar to be visible."
        )

    @needs_scip
    @pytest.mark.asyncio
    async def test_scip_caller_ids_resolve_to_get_callers(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """
        Regression test for SCIP caller_id mismatch (commit d32f286).

        SCIP edges must store tree-sitter-format caller_ids so that the
        get_callers() JOIN (n.id = e.caller_id) succeeds. If caller_ids
        contain SCIP package hashes, get_callers() returns [].
        """
        _write_project(tmp_path, self.FIXTURE_WITH_IMPORT)
        await indexer.index_project(str(tmp_path), project_id="test_scip")

        # read_config calls json.load — json.load should see read_config as a caller
        # via SCIP reference edges
        callers = await db.get_callers("read_config", project_id="test_scip")
        # The function may have no internal callers, but it must not error.
        # The key invariant: caller_ids in stored edges must all be valid node IDs.
        all_node_ids = {n["id"] for n in await db.get_all_nodes("test_scip")}
        async with db._pool.acquire() as conn:
            bad_edges = await conn.fetch(
                """
                SELECT e.caller_id FROM edges e
                WHERE e.project_id = 'test_scip'
                  AND e.caller_id NOT IN (
                      SELECT id FROM nodes WHERE project_id = 'test_scip'
                  )
                LIMIT 5
                """
            )
        assert len(bad_edges) == 0, (
            f"Found {len(bad_edges)} edges with caller_ids that don't match any node. "
            f"Examples: {[r['caller_id'] for r in bad_edges[:3]]}. "
            "This is the SCIP caller_id mismatch bug — caller_id must use "
            "tree-sitter format (module.ClassName.method), not SCIP hash format."
        )


# ── get_project_home output integrity ─────────────────────────────────────────

class TestProjectHomeAfterIndex:
    """
    get_project_home() aggregates a lot of data. After a real index these tests
    verify the output stays within usable bounds — catching the 898-connections
    blowup that made the response exceed the MCP token limit.
    """

    @pytest.mark.asyncio
    async def test_project_home_connections_are_bounded(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        """
        Regression for get_project_home connections bloat (commit d32f286).

        With SCIP external nodes each becoming their own subsystem, a small
        project produced 898 connections — exceeding MCP token limits. After
        the fix, external subsystems are excluded from the wiring diagram.
        """
        _write_project(tmp_path, FIXTURE_SIMPLE)
        result = await indexer.index_project(str(tmp_path), project_id="test_proj")

        home = await db.get_project_home_data("test_proj")
        connections = home.get("connections", [])
        # A 2-file project has at most a handful of internal subsystem pairs.
        # 50 is already generous — if this hits 100+ something has broken.
        assert len(connections) < 50, (
            f"get_project_home returned {len(connections)} connections for a "
            f"2-file fixture project. External subsystems may be leaking into "
            f"the wiring diagram (the 898-connection bug)."
        )

    @pytest.mark.asyncio
    async def test_project_home_subsystems_are_present(
        self, indexer: Indexer, db: CallGraphDB, tmp_path: Path
    ):
        _write_project(tmp_path, FIXTURE_SIMPLE)
        await indexer.index_project(str(tmp_path), project_id="test_proj")

        home = await db.get_project_home_data("test_proj")
        subsystems = home.get("subsystems", [])
        assert len(subsystems) > 0
        subsystem_names = {s["name"] for s in subsystems}
        # src.app and src.calculator should be distinct subsystems
        assert any("src" in s for s in subsystem_names)
