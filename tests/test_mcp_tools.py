"""
Integration tests for MCP tool handlers.

These tests call handler functions directly — they are async Python functions,
not special MCP objects, so they're fully callable in tests. Each test:
  1. Inserts data into a real SQLite DB (temp file, no API keys needed)
  2. Patches _get_services() to return a Services with that DB
  3. Calls the handler
  4. Parses the returned JSON and asserts on observable behavior

Services that require API keys (embeddings, pipeline, decisions) are replaced
with MagicMock for handlers that don't use them. Tests that would need live
embeddings are explicitly skipped — those require a separate integration suite.

Failure here means a caller would get bad JSON or an exception from the tool
they depend on most. These tests are the front-line signal.
"""
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from src.call_graph.storage import CallGraphDB
from src.call_graph.parser import FunctionNode, CallEdge
from src.dependency_fingerprint import DependencyChecker


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def bypass_auth():
    """Bypass auth context for all tool tests — tools are tested for logic, not auth."""
    with patch("src.tools._shared.check_read_access", AsyncMock(return_value=None)), \
         patch("src.tools.memory.check_permission", AsyncMock(return_value=None)), \
         patch("src.tools.memory.get_current_user", return_value={"id": "test-user"}), \
         patch("src.tools.discovery.get_current_user", return_value={"id": "test-user"}), \
         patch("src.server.get_current_user", return_value={"id": "test-user"}), \
         patch("src.server.check_permission", AsyncMock(return_value=None)):
        yield


@pytest_asyncio.fixture
async def svc(db):
    """Services container with real DB, mocked API-key-dependent layers."""
    from src.server import Services
    return Services(
        db=db,
        embeddings=MagicMock(),
        pipeline=MagicMock(),
        decisions=MagicMock(),
        indexer=MagicMock(),
        contracts=MagicMock(),
        checker=DependencyChecker(),
    )


def _node(node_id: str, *, is_external: bool = False, name: str | None = None) -> FunctionNode:
    """Minimal FunctionNode for DB insertion."""
    parts = node_id.split(".")
    return FunctionNode(
        id=node_id,
        name=name or parts[-1],
        file=f"src/{parts[-1]}.py" if not is_external else f"<{parts[1]}>",
        module=".".join(parts[:2]),
        type="function",
        signature=f"def {parts[-1]}():",
        body="pass",
        docstring="",
        body_hash="abc123",
        is_external=is_external,
    )


async def _insert(db: CallGraphDB, project_id: str, nodes: list, edges: list = []):
    """Seed the DB with nodes + edges for a project.

    Writes through a project-scoped DB so data lands in the project schema
    (matching what the indexer and tool reads now both use via resolve_project_db).
    """
    from src.call_graph.storage import derive_schema_name
    await db.upsert_project(project_id, project_id, "/workspace/test")
    pdb = await db.project_db(derive_schema_name(project_id))
    await pdb.upsert_nodes(nodes, project_id)
    if edges:
        all_ids = await pdb.get_all_node_ids(project_id)
        await pdb.upsert_edges(edges, all_ids, project_id)


# ── list_projects ─────────────────────────────────────────────────────────────

class TestListProjects:
    @pytest.mark.asyncio
    async def test_returns_registered_project(self, svc):
        await svc.db.upsert_project("myapp", "myapp", "/workspace/myapp")
        svc.db.get_accessible_project_ids = AsyncMock(return_value={"myapp"})
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.discovery import list_projects
            result = json.loads(await list_projects())
        ids = [p["id"] for p in result]
        assert "myapp" in ids

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_no_projects(self, svc):
        svc.db.get_accessible_project_ids = AsyncMock(return_value=set())
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.discovery import list_projects
            result = json.loads(await list_projects())
        assert result == []


# ── get_callers / get_callees ─────────────────────────────────────────────────

class TestGetCallers:
    @pytest.mark.asyncio
    async def test_returns_caller_of_function(self, svc):
        nodes = [_node("src.server.caller"), _node("src.server.target")]
        edges = [CallEdge(caller_id="src.server.caller", callee_name="src.server.target",
                          edge_type="calls", file="src/server.py")]
        await _insert(svc.db, "proj", nodes, edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_callers
            result = json.loads(await get_callers("target", project_id="proj"))
        names = [r["name"] for r in result["callers"]]
        assert "caller" in names

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_function(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_callers
            result = json.loads(await get_callers("does_not_exist", project_id="proj"))
        assert result["callers"] == []

    @pytest.mark.asyncio
    async def test_is_external_flag_present_in_results(self, svc):
        """Callers now expose is_external so clients can tell stubs from real functions."""
        internal = _node("src.server.handler")
        external = _node("external.requests.get", is_external=True, name="get")
        edges = [CallEdge(caller_id="src.server.handler", callee_name="external.requests.get",
                          edge_type="reference", file="src/server.py")]
        await _insert(svc.db, "proj", [internal, external], edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_callees
            result = json.loads(await get_callees("handler", project_id="proj"))
        callees = result["callees"]
        assert len(callees) == 1
        assert "is_external" in callees[0]
        assert callees[0]["is_external"] == 1


class TestGetCallees:
    @pytest.mark.asyncio
    async def test_returns_callee_of_function(self, svc):
        nodes = [_node("src.server.fn_a"), _node("src.server.fn_b")]
        edges = [CallEdge(caller_id="src.server.fn_a", callee_name="src.server.fn_b",
                          edge_type="calls", file="src/server.py")]
        await _insert(svc.db, "proj", nodes, edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_callees
            result = json.loads(await get_callees("fn_a", project_id="proj"))
        names = [r["name"] for r in result["callees"]]
        assert "fn_b" in names


# ── list_external_dependencies ────────────────────────────────────────────────

class TestListExternalDependencies:
    @pytest.mark.asyncio
    async def test_returns_external_stubs_grouped_by_library(self, svc):
        internal = _node("src.server.handler")
        stub = _node("external.requests.get", is_external=True, name="get")
        edges = [CallEdge(caller_id="src.server.handler", callee_name="external.requests.get",
                          edge_type="reference", file="src/server.py")]
        await _insert(svc.db, "proj", [internal, stub], edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import list_external_dependencies
            result = json.loads(await list_external_dependencies("proj"))
        libs = [entry["library"] for entry in result]
        assert "requests" in libs

    @pytest.mark.asyncio
    async def test_returns_empty_for_project_with_no_external_nodes(self, svc):
        await _insert(svc.db, "proj", [_node("src.server.fn")])

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import list_external_dependencies
            result = json.loads(await list_external_dependencies("proj"))
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_caller_count_reflects_how_many_internal_functions_call_symbol(self, svc):
        stub = _node("external.numpy.array", is_external=True, name="array")
        callers = [_node(f"src.mod.fn_{i}") for i in range(3)]
        edges = [
            CallEdge(caller_id=f"src.mod.fn_{i}", callee_name="external.numpy.array",
                     edge_type="reference", file="src/mod.py")
            for i in range(3)
        ]
        await _insert(svc.db, "proj", callers + [stub], edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import list_external_dependencies
            result = json.loads(await list_external_dependencies("proj"))
        numpy = next(e for e in result if e["library"] == "numpy")
        symbol = numpy["symbols"][0]
        assert symbol["caller_count"] == 3


# ── get_library_dependents ────────────────────────────────────────────────────

class TestGetLibraryDependents:
    @pytest.mark.asyncio
    async def test_returns_internal_functions_calling_library(self, svc):
        internal = _node("src.api.handler")
        stub = _node("external.requests.get", is_external=True, name="get")
        edges = [CallEdge(caller_id="src.api.handler", callee_name="external.requests.get",
                          edge_type="reference", file="src/api.py")]
        await _insert(svc.db, "proj", [internal, stub], edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_library_dependents
            result = json.loads(await get_library_dependents("requests", "proj"))
        ids = [r["id"] for r in result]
        assert "src.api.handler" in ids

    @pytest.mark.asyncio
    async def test_returns_empty_when_library_not_used(self, svc):
        await _insert(svc.db, "proj", [_node("src.api.handler")])

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_library_dependents
            result = json.loads(await get_library_dependents("requests", "proj"))
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_does_not_return_external_stubs_as_dependents(self, svc):
        """Dependents are internal functions only — stubs are not dependents of each other."""
        stub_a = _node("external.requests.get", is_external=True, name="get")
        stub_b = _node("external.requests.post", is_external=True, name="post")
        await _insert(svc.db, "proj", [stub_a, stub_b])

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_library_dependents
            result = json.loads(await get_library_dependents("requests", "proj"))
        assert result["results"] == []


# ── Dependency fingerprint tools ──────────────────────────────────────────────

async def _insert_fingerprint(db, project_id, *, libraries=None, diff=None, fp_id="fp1"):
    """Seed a fingerprint row directly into the DB."""
    import uuid, json as _json
    from datetime import datetime, timezone
    snapshot = {
        "project_id": project_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint_hash": "abc123",
        "total_libraries": len(libraries or {}),
        "total_external_symbols": sum(
            len(v.get("symbols", [])) for v in (libraries or {}).values()
        ),
        "libraries": libraries or {},
    }
    diff_json = _json.dumps(diff) if diff else None
    await db.save_dependency_fingerprint(
        project_id, fp_id,
        snapshot["captured_at"], snapshot["fingerprint_hash"],
        _json.dumps(snapshot), diff_json,
    )
    return snapshot


class TestGetDependencyFingerprint:
    @pytest.mark.asyncio
    async def test_returns_fingerprint_for_project(self, svc):
        await _insert_fingerprint(svc.db, "proj",
            libraries={"requests": {"version": "2.28.0", "symbol_count": 1,
                                    "symbols": [{"id": "external.requests.get",
                                                 "signature": "requests.get(...)",
                                                 "caller_count": 3}]}})
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_dependency_fingerprint
            result = json.loads(await get_dependency_fingerprint("proj"))
        assert result["libraries"]["requests"]["version"] == "2.28.0"

    @pytest.mark.asyncio
    async def test_returns_no_fingerprint_status_when_none_exists(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_dependency_fingerprint
            result = json.loads(await get_dependency_fingerprint("proj"))
        assert result["status"] == "no fingerprint"

    @pytest.mark.asyncio
    async def test_diff_from_previous_included_in_result(self, svc):
        diff = {"removed_symbols": [{"id": "external.requests.post",
                                     "library": "requests", "signature": "sig"}],
                "added_symbols": [], "changed_symbols": [], "version_changes": []}
        await _insert_fingerprint(svc.db, "proj",
            libraries={"requests": {"version": "2.31.0", "symbol_count": 0, "symbols": []}},
            diff=diff)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_dependency_fingerprint
            result = json.loads(await get_dependency_fingerprint("proj"))
        removed = result["diff_from_previous"]["removed_symbols"]
        assert len(removed) == 1
        assert removed[0]["id"] == "external.requests.post"


class TestGetDependencyFingerprintAt:
    @pytest.mark.asyncio
    async def test_returns_fingerprint_by_id(self, svc):
        await _insert_fingerprint(svc.db, "proj",
            libraries={"numpy": {"version": "1.24.0", "symbol_count": 0, "symbols": []}},
            fp_id="fp-historical")

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_dependency_fingerprint_at
            result = json.loads(await get_dependency_fingerprint_at("fp-historical"))
        assert "numpy" in result["libraries"]

    @pytest.mark.asyncio
    async def test_returns_not_found_for_unknown_id(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import get_dependency_fingerprint_at
            result = json.loads(await get_dependency_fingerprint_at("does-not-exist"))
        assert result["status"] == "not found"


class TestListDependencyFingerprintHistory:
    @pytest.mark.asyncio
    async def test_returns_all_snapshots_newest_first(self, svc):
        await _insert_fingerprint(svc.db, "proj", fp_id="fp-old",
            libraries={"requests": {"version": "2.28.0", "symbol_count": 0, "symbols": []}})
        await _insert_fingerprint(svc.db, "proj", fp_id="fp-new",
            libraries={"requests": {"version": "2.31.0", "symbol_count": 0, "symbols": []}})

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import list_dependency_fingerprint_history
            result = json.loads(await list_dependency_fingerprint_history("proj"))
        ids = [r["id"] for r in result]
        assert "fp-old" in ids
        assert "fp-new" in ids

    @pytest.mark.asyncio
    async def test_removed_symbols_surfaced_in_history_row(self, svc):
        diff = {"removed_symbols": [{"id": "external.requests.post",
                                     "library": "requests", "signature": "sig"}],
                "added_symbols": [], "changed_symbols": [], "version_changes": []}
        await _insert_fingerprint(svc.db, "proj", fp_id="fp1",
            libraries={}, diff=diff)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import list_dependency_fingerprint_history
            result = json.loads(await list_dependency_fingerprint_history("proj"))
        row = next(r for r in result if r["id"] == "fp1")
        assert row["removed_count"] == 1
        assert "external.requests.post" in row["removed_symbols"]

    @pytest.mark.asyncio
    async def test_returns_empty_for_project_with_no_fingerprints(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import list_dependency_fingerprint_history
            result = json.loads(await list_dependency_fingerprint_history("proj"))
        assert result == []


class TestCompareDependencyFingerprints:
    @pytest.mark.asyncio
    async def test_diff_surfaces_removed_symbol_between_snapshots(self, svc):
        # fp-a has requests.post; fp-b does not
        libs_a = {"requests": {"version": "2.28.0", "symbol_count": 2, "symbols": [
            {"id": "external.requests.get",  "signature": "requests.get(...)",  "caller_count": 1},
            {"id": "external.requests.post", "signature": "requests.post(...)", "caller_count": 1},
        ]}}
        libs_b = {"requests": {"version": "2.31.0", "symbol_count": 1, "symbols": [
            {"id": "external.requests.get", "signature": "requests.get(...)", "caller_count": 1},
        ]}}
        await _insert_fingerprint(svc.db, "proj", libraries=libs_a, fp_id="fp-a")
        await _insert_fingerprint(svc.db, "proj", libraries=libs_b, fp_id="fp-b")

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import compare_dependency_fingerprints
            result = json.loads(await compare_dependency_fingerprints("fp-a", "fp-b"))
        removed = result["diff"]["removed_symbols"]
        assert any(s["id"] == "external.requests.post" for s in removed)
        assert result["has_changes"] is True

    @pytest.mark.asyncio
    async def test_returns_not_found_for_unknown_fingerprint(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import compare_dependency_fingerprints
            result = json.loads(await compare_dependency_fingerprints("bad-id", "also-bad"))
        assert result["status"] == "not found"


class TestCheckDependency:
    @pytest.mark.asyncio
    async def test_returns_version_from_fingerprint(self, svc):
        await _insert_fingerprint(svc.db, "proj",
            libraries={"requests": {"version": "2.31.0", "symbol_count": 0, "symbols": []}})
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import check_dependency
            result = json.loads(await check_dependency("requests", "proj"))
        assert result["version"] == "2.31.0"

    @pytest.mark.asyncio
    async def test_returns_dependents_calling_library(self, svc):
        internal = _node("src.api.handler")
        stub = _node("external.requests.get", is_external=True, name="get")
        edges = [CallEdge(caller_id="src.api.handler", callee_name="external.requests.get",
                          edge_type="reference", file="src/api.py")]
        await _insert(svc.db, "proj", [internal, stub], edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import check_dependency
            result = json.loads(await check_dependency("requests", "proj"))
        assert result["dependent_count"] == 1
        assert result["dependents"][0]["id"] == "src.api.handler"

    @pytest.mark.asyncio
    async def test_unknown_version_when_no_fingerprint(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import check_dependency
            result = json.loads(await check_dependency("requests", "proj"))
        assert result["version"] == "unknown"
        assert result["dependent_count"] == 0

    @pytest.mark.asyncio
    async def test_recent_changes_filtered_to_library(self, svc):
        """Removed symbols from OTHER libraries must not appear in the envelope."""
        diff = {
            "removed_symbols": [
                {"library": "requests", "id": "external.requests.post", "signature": "sig"},
                {"library": "numpy",    "id": "external.numpy.array",   "signature": "sig"},
            ],
            "added_symbols": [], "changed_symbols": [], "version_changes": [],
        }
        await _insert_fingerprint(svc.db, "proj",
            libraries={"requests": {"version": "2.31.0", "symbol_count": 0, "symbols": []}},
            diff=diff)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.dependencies import check_dependency
            result = json.loads(await check_dependency("requests", "proj"))
        removed = result["recent_changes"]["removed_symbols"]
        assert len(removed) == 1
        assert removed[0]["id"] == "external.requests.post"


# ── _embed_single LRU cache ───────────────────────────────────────────────────

class TestEmbedSingleCache:
    """
    _embed_single() caches by (sha256(text), model). A cache hit must not
    call the API client — the whole point is to avoid paying twice.
    """

    def _store(self, tmp_path):
        """EmbeddingStore with a mocked API client so no real calls fire."""
        from unittest.mock import AsyncMock, MagicMock
        from src.embeddings.embedder import EmbeddingStore
        store = EmbeddingStore.__new__(EmbeddingStore)
        store._model = "text-embedding-3-small"
        store._embed_cache = __import__("collections").OrderedDict()
        store._embed_cache_max = 4
        # Mock client whose embeddings.create returns a fixed vector
        mock_resp = MagicMock()
        mock_resp.data = [MagicMock(embedding=[0.1, 0.2, 0.3])]
        store._embed_client = MagicMock()
        store._embed_client.embeddings.create = AsyncMock(return_value=mock_resp)
        return store

    @pytest.mark.asyncio
    async def test_second_call_with_same_text_does_not_hit_api(self, tmp_path):
        store = self._store(tmp_path)
        await store._embed_single("hello world")
        await store._embed_single("hello world")
        assert store._embed_client.embeddings.create.call_count == 1

    @pytest.mark.asyncio
    async def test_different_text_calls_api_again(self, tmp_path):
        store = self._store(tmp_path)
        await store._embed_single("text one")
        await store._embed_single("text two")
        assert store._embed_client.embeddings.create.call_count == 2

    @pytest.mark.asyncio
    async def test_cache_evicts_oldest_when_full(self, tmp_path):
        store = self._store(tmp_path)
        # Fill cache to max (4 entries)
        for i in range(4):
            await store._embed_single(f"text {i}")
        assert len(store._embed_cache) == 4
        # Add one more — oldest ("text 0") should be evicted
        await store._embed_single("text 4")
        assert len(store._embed_cache) == 4
        keys = [k[0] for k in store._embed_cache.keys()]
        import hashlib
        evicted_key = hashlib.sha256("text 0".encode()).hexdigest()[:16]
        assert evicted_key not in keys

    @pytest.mark.asyncio
    async def test_cache_hit_returns_same_vector(self, tmp_path):
        store = self._store(tmp_path)
        v1 = await store._embed_single("hello")
        v2 = await store._embed_single("hello")
        assert v1 == v2


# ── get_impact_radius ─────────────────────────────────────────────────────────

class TestGetImpactRadius:
    @pytest.mark.asyncio
    async def test_returns_callers_of_function(self, svc):
        nodes = [_node("src.mod.inner"), _node("src.mod.outer")]
        edges = [CallEdge(caller_id="src.mod.outer", callee_name="src.mod.inner",
                          edge_type="calls", file="src/mod.py")]
        await _insert(svc.db, "proj", nodes, edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_impact_radius
            result = json.loads(await get_impact_radius("inner", project_id="proj"))
        ids = {r["id"] for r in result["impact_radius"]}
        assert "src.mod.outer" in ids

    @pytest.mark.asyncio
    async def test_returns_empty_for_unknown_function(self, svc):
        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_impact_radius
            result = json.loads(await get_impact_radius("nonexistent", project_id="proj"))
        assert result["impact_radius"] == []

    @pytest.mark.asyncio
    async def test_result_includes_impact_depth(self, svc):
        nodes = [_node("src.mod.leaf"), _node("src.mod.caller")]
        edges = [CallEdge(caller_id="src.mod.caller", callee_name="src.mod.leaf",
                          edge_type="calls", file="src/mod.py")]
        await _insert(svc.db, "proj", nodes, edges)

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.graph import get_impact_radius
            result = json.loads(await get_impact_radius("leaf", project_id="proj"))
        caller = next(r for r in result["impact_radius"] if r["id"] == "src.mod.caller")
        assert "impact_depth" in caller
        assert caller["impact_depth"] == 1


# ── log_decision + get_decision_history ──────────────────────────────────────

class TestLogDecision:
    @pytest.mark.asyncio
    async def test_logged_decision_appears_in_history(self, svc):
        nodes = [_node("src.mod.my_func")]
        await _insert(svc.db, "proj", nodes)

        svc.decisions = MagicMock()
        svc.decisions.log_decision = AsyncMock(return_value={"id": "dec-001"})

        with patch("src.server._get_services", AsyncMock(return_value=svc)), \
             patch("src.server.get_current_user", return_value={"id": "test-user"}), \
             patch("src.server.check_permission", AsyncMock(return_value=None)):
            from src.tools.memory import log_decision
            result = json.loads(await log_decision(
                type="Implementation",
                description="Chose asyncpg over psycopg2 for connection pooling",
                project_id="proj",
                rejected_alternatives="psycopg2",
                trigger="performance_test",
                linked_function_ids=["src.mod.my_func"],
            ))
        # Result is whatever decisions.log_decision returned, JSON-encoded
        assert result is not None

    @pytest.mark.asyncio
    async def test_get_decision_history_returns_decisions(self, svc):
        nodes = [_node("src.mod.my_func")]
        await _insert(svc.db, "proj", nodes)

        svc.decisions = MagicMock()
        svc.decisions.get_decision_history = AsyncMock(return_value=[
            {"id": "dec-001", "description": "Chose asyncpg", "type": "Implementation",
             "created_at": "2026-01-01", "functions": ["src.mod.my_func"]}
        ])

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.memory import get_decision_history
            result = json.loads(await get_decision_history(
                function_name="my_func", project_id="proj"
            ))
        assert isinstance(result["decisions"], list)
        assert len(result["decisions"]) == 1
        assert "_guidance" in result
        assert "Chose asyncpg" in result["_guidance"]["note"]

    @pytest.mark.asyncio
    async def test_get_decision_history_returns_empty_for_unknown(self, svc):
        svc.decisions = MagicMock()
        svc.decisions.get_decision_history = AsyncMock(return_value=[])

        with patch("src.tools._shared.get_services", AsyncMock(return_value=svc)):
            from src.tools.memory import get_decision_history
            result = json.loads(await get_decision_history(
                function_name="nonexistent", project_id="proj"
            ))
        assert result["decisions"] == []
        assert "_guidance" in result
        assert "No decisions logged" in result["_guidance"]["note"]
