"""
Tests for the core storage query operations that power every MCP call graph tool.

find_node_by_name  —  three-step fuzzy lookup (exact id → exact name → suffix)
_resolve_callee    —  bare callee name → stored node ID
get_impact_radius  —  BFS traversal outward to find what breaks if X changes

WHY THESE NEED TESTS
---------------------
These three functions are the connective tissue between the indexer (which writes)
and every MCP tool (which reads). They have non-trivial logic:

  find_node_by_name has a known ambiguity bug: when multiple nodes share a
  bare name (e.g. 'index_project' exists as both an MCP tool handler AND
  the Indexer method), step 1 returns the tool handler (exact name match)
  and step 2 is never tried. get_callers() then finds no callers for the
  Indexer method. These tests pin the exact behavior so the bug is visible
  and a fix can be validated.

  _resolve_callee is where call graph edge quality is determined. An ambiguous
  callee name falls back to the bare string — making the edge unresolvable.

  get_impact_radius does BFS traversal — depth handling and cross-project
  isolation are subtle and untested.

Uses the `db` fixture (real Postgres, persistent). Each test uses `project_id`
to get its own namespace — no cleanup needed between runs.
"""
import pytest
import pytest_asyncio
from src.call_graph.parser import FunctionNode, CallEdge
from src.call_graph.storage import CallGraphDB, _resolve_callee


# ── Helpers ───────────────────────────────────────────────────────────────────

def _node(node_id: str, *, name: str | None = None, is_external: bool = False) -> FunctionNode:
    parts = node_id.split(".")
    return FunctionNode(
        id=node_id,
        name=name or parts[-1],
        file=f"/project/{parts[0]}.py",
        module=".".join(parts[:2]) if len(parts) >= 2 else parts[0],
        type="function",
        signature=f"def {parts[-1]}():",
        body="pass",
        docstring="",
        body_hash="abc123",
        is_external=is_external,
    )


def _edge(caller: str, callee_name: str, edge_type: str = "calls") -> CallEdge:
    return CallEdge(caller_id=caller, callee_name=callee_name,
                    edge_type=edge_type, file="/project/mod.py")


async def _seed(db: CallGraphDB, project_id: str, nodes: list, edges: list = []):
    await db.upsert_project(project_id, project_id, "/project")
    await db.upsert_nodes(nodes, project_id)
    if edges:
        all_ids = await db.get_all_node_ids(project_id)
        await db.upsert_edges(edges, all_ids, project_id)


# ── find_node_by_name — three-step lookup ────────────────────────────────────

class TestFindNodeByName:
    """
    find_node_by_name drives get_callers, get_callees, and get_impact_radius.
    Three lookup strategies, each falling through to the next:
      1. Exact id OR exact name match
      2. Suffix match on id (id LIKE '%.{name}')
    """

    @pytest.mark.asyncio
    async def test_exact_id_match_returns_node(self, db: CallGraphDB, project_id: str):
        await _seed(db, project_id, [_node("src.mod.my_func")])
        results = await db.find_node_by_name("src.mod.my_func", project_id)
        assert len(results) == 1
        assert results[0]["id"] == "src.mod.my_func"

    @pytest.mark.asyncio
    async def test_exact_name_match_returns_node(self, db: CallGraphDB, project_id: str):
        await _seed(db, project_id, [_node("src.mod.my_func", name="my_func")])
        results = await db.find_node_by_name("my_func", project_id)
        assert len(results) == 1
        assert results[0]["id"] == "src.mod.my_func"

    @pytest.mark.asyncio
    async def test_suffix_match_returns_node_when_no_exact(self, db: CallGraphDB, project_id: str):
        """
        'Indexer.index_project' doesn't match name='index_project',
        but its id ends in '.index_project' → suffix match finds it.
        """
        await _seed(db, project_id, [_node("src.indexer.Indexer.index_project",
                                        name="Indexer.index_project")])
        results = await db.find_node_by_name("index_project", project_id)
        assert len(results) == 1
        assert "index_project" in results[0]["id"]

    @pytest.mark.asyncio
    async def test_unknown_name_returns_empty(self, db: CallGraphDB, project_id: str):
        await _seed(db, project_id, [_node("src.mod.fn")])
        results = await db.find_node_by_name("does_not_exist", project_id)
        assert results == []

    @pytest.mark.asyncio
    async def test_project_scoping_excludes_other_projects(self, db: CallGraphDB, project_id: str):
        proj_a = f"{project_id}a"
        proj_b = f"{project_id}b"
        await _seed(db, proj_a, [_node("src.mod.fn", name="fn")])
        await _seed(db, proj_b, [_node("src.mod.fn", name="fn")])
        # Searching in proj_a must not return proj_b nodes
        results = await db.find_node_by_name("fn", proj_a)
        assert all(r["project_id"] == proj_a for r in results)

    @pytest.mark.asyncio
    async def test_no_project_scope_searches_all_projects(self, db: CallGraphDB, project_id: str):
        proj_a = f"{project_id}a"
        proj_b = f"{project_id}b"
        await _seed(db, proj_a, [_node("src.mod.fn", name="fn")])
        await _seed(db, proj_b, [_node("src.mod.fn", name="fn")])
        results = await db.find_node_by_name("fn")  # no project_id
        found_projects = {r["project_id"] for r in results}
        assert proj_a in found_projects
        assert proj_b in found_projects

    @pytest.mark.asyncio
    async def test_both_exact_and_suffix_matches_returned(self, db: CallGraphDB, project_id: str):
        """
        Regression test for the fixed ambiguity bug.

        Previously: step 1 (exact name match) short-circuited and step 2 (suffix
        match) was never reached. Querying 'index_project' returned only the tool
        handler (name='index_project'), silently omitting the Indexer method whose
        id ends in '.index_project'. get_callers("index_project") returned wrong results.

        Now: both steps always run. The tool handler (exact name) comes first,
        the method (suffix match) is appended. All graph traversal tools benefit
        from the complete candidate set.
        """
        tool_handler = _node("src.server.index_project", name="index_project")
        indexer_method = _node("src.indexer.Indexer.index_project",
                                name="Indexer.index_project")
        await _seed(db, project_id, [tool_handler, indexer_method])

        results = await db.find_node_by_name("index_project", project_id)
        ids = {r["id"] for r in results}
        # Both nodes must now be returned — the fix is complete
        assert "src.server.index_project" in ids, "Exact name match must be present"
        assert "src.indexer.Indexer.index_project" in ids, (
            "Suffix match must be present — step 2 now always runs even when step 1 "
            "finds results. This was the ambiguity bug: previously only the tool "
            "handler was returned, making get_callers silent for the Indexer method."
        )

    @pytest.mark.asyncio
    async def test_exact_match_comes_before_suffix_match(self, db: CallGraphDB, project_id: str):
        """Step 1 results appear first so callers that take [0] get the best match."""
        tool_handler = _node("src.server.index_project", name="index_project")
        indexer_method = _node("src.indexer.Indexer.index_project",
                                name="Indexer.index_project")
        await _seed(db, project_id, [tool_handler, indexer_method])

        results = await db.find_node_by_name("index_project", project_id)
        # The exact name match must be first — get_function_context takes [0]
        assert results[0]["id"] == "src.server.index_project"

    @pytest.mark.asyncio
    async def test_no_duplicates_when_exact_also_matches_suffix(self, db: CallGraphDB, project_id: str):
        """A node that matches both steps (full id passed) appears exactly once."""
        await _seed(db, project_id, [_node("src.mod.my_func", name="my_func")])
        results = await db.find_node_by_name("src.mod.my_func", project_id)
        ids = [r["id"] for r in results]
        assert ids.count("src.mod.my_func") == 1

    @pytest.mark.asyncio
    async def test_sql_special_chars_in_name_handled_safely(self, db: CallGraphDB, project_id: str):
        """Names with % and _ must not cause LIKE injection."""
        await _seed(db, project_id, [_node("src.mod.fn_a"), _node("src.mod.fn_b")])
        # Searching for "fn_%" should NOT match fn_a or fn_b via LIKE wildcard expansion
        results = await db.find_node_by_name("fn_%", project_id)
        assert results == []


# ── _resolve_callee ──────────────────────────────────────────────────────────

class TestResolveCallee:
    """
    _resolve_callee converts a bare callee name to a stored node ID.
    This determines whether call edges point to real nodes or become dangling strings.
    """

    def test_exact_id_match_resolves(self):
        ids = {"src.mod.helper", "src.mod.caller"}
        assert _resolve_callee("src.mod.helper", ids) == "src.mod.helper"

    def test_unique_suffix_match_resolves(self):
        ids = {"src.mod.helper", "src.other.caller"}
        assert _resolve_callee("helper", ids) == "src.mod.helper"

    def test_ambiguous_suffix_falls_back_to_bare_name(self):
        """
        When multiple nodes end with '.helper', _resolve_callee can't pick one
        and falls back to the bare string. The edge stored is dangling.
        This is an open limitation — pinned here so it's visible.
        """
        ids = {"src.mod.helper", "src.utils.helper"}
        result = _resolve_callee("helper", ids)
        assert result == "helper"  # bare name fallback — edge is dangling

    def test_no_matching_node_falls_back_to_bare_name(self):
        ids = {"src.mod.fn"}
        result = _resolve_callee("unknown_fn", ids)
        assert result == "unknown_fn"

    def test_empty_all_ids_falls_back_to_bare_name(self):
        assert _resolve_callee("fn", set()) == "fn"

    def test_resolution_is_case_sensitive(self):
        """IDs are stored verbatim — casing matters for exact matches."""
        ids = {"src.mod.MyClass"}
        result_exact = _resolve_callee("src.mod.MyClass", ids)
        result_lower = _resolve_callee("src.mod.myclass", ids)
        assert result_exact == "src.mod.MyClass"
        assert result_lower == "src.mod.myclass"  # no match → bare string


# ── get_impact_radius ─────────────────────────────────────────────────────────

class TestGetImpactRadius:
    """
    get_impact_radius answers "what breaks if I change X?" via BFS traversal.
    Depth-1 = direct callers. Depth-2 = callers of callers. Etc.
    """

    @pytest.mark.asyncio
    async def test_no_callers_returns_only_target(self, db: CallGraphDB, project_id: str):
        """The target function is returned at impact_depth=0 even with no callers."""
        await _seed(db, project_id, [_node("src.mod.leaf")])
        result = await db.get_impact_radius("leaf", depth=2, project_id=project_id)
        ids = {r["id"] for r in result}
        assert "src.mod.leaf" in ids
        assert all(r["impact_depth"] == 0 for r in result)

    @pytest.mark.asyncio
    async def test_direct_caller_at_depth_one(self, db: CallGraphDB, project_id: str):
        nodes = [_node("src.mod.leaf"), _node("src.mod.caller")]
        edges = [_edge("src.mod.caller", "leaf")]
        await _seed(db, project_id, nodes, edges)

        result = await db.get_impact_radius("leaf", depth=1, project_id=project_id)
        ids = {r["id"] for r in result}
        assert "src.mod.caller" in ids

    @pytest.mark.asyncio
    async def test_transitive_caller_at_depth_two(self, db: CallGraphDB, project_id: str):
        """outer → middle → inner: impact from inner reaches outer at depth 2."""
        nodes = [_node("src.mod.inner"), _node("src.mod.middle"), _node("src.mod.outer")]
        edges = [
            _edge("src.mod.middle", "inner"),
            _edge("src.mod.outer", "middle"),
        ]
        await _seed(db, project_id, nodes, edges)

        result = await db.get_impact_radius("inner", depth=2, project_id=project_id)
        ids = {r["id"] for r in result}
        assert "src.mod.middle" in ids
        assert "src.mod.outer" in ids

    @pytest.mark.asyncio
    async def test_depth_one_excludes_depth_two_callers(self, db: CallGraphDB, project_id: str):
        nodes = [_node("src.mod.inner"), _node("src.mod.middle"), _node("src.mod.outer")]
        edges = [
            _edge("src.mod.middle", "inner"),
            _edge("src.mod.outer", "middle"),
        ]
        await _seed(db, project_id, nodes, edges)

        result = await db.get_impact_radius("inner", depth=1, project_id=project_id)
        ids = {r["id"] for r in result}
        assert "src.mod.middle" in ids
        assert "src.mod.outer" not in ids  # too deep

    @pytest.mark.asyncio
    async def test_impact_depth_field_reflects_distance(self, db: CallGraphDB, project_id: str):
        nodes = [_node("src.mod.inner"), _node("src.mod.middle"), _node("src.mod.outer")]
        edges = [
            _edge("src.mod.middle", "inner"),
            _edge("src.mod.outer", "middle"),
        ]
        await _seed(db, project_id, nodes, edges)

        result = await db.get_impact_radius("inner", depth=2, project_id=project_id)
        by_id = {r["id"]: r["impact_depth"] for r in result}
        assert by_id["src.mod.middle"] == 1
        assert by_id["src.mod.outer"] == 2

    @pytest.mark.asyncio
    async def test_scoped_to_project(self, db: CallGraphDB, project_id: str):
        """Impact radius must not cross project boundaries."""
        proj_a = f"{project_id}a"
        proj_b = f"{project_id}b"
        await _seed(db, proj_a, [_node("src.mod.fn"), _node("src.mod.caller")],
                    [_edge("src.mod.caller", "fn")])
        await _seed(db, proj_b, [_node("src.mod.fn"), _node("src.mod.caller")],
                    [_edge("src.mod.caller", "fn")])

        result = await db.get_impact_radius("fn", depth=1, project_id=proj_a)
        assert all(r["project_id"] == proj_a for r in result)

    @pytest.mark.asyncio
    async def test_cycle_does_not_infinite_loop(self, db: CallGraphDB, project_id: str):
        """A → B → A cycle must terminate at depth limit, not loop forever."""
        nodes = [_node("src.mod.a"), _node("src.mod.b")]
        edges = [_edge("src.mod.a", "b"), _edge("src.mod.b", "a")]
        await _seed(db, project_id, nodes, edges)

        result = await db.get_impact_radius("a", depth=5, project_id=project_id)
        # Should return without hanging — exact count may vary
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_results_sorted_by_depth(self, db: CallGraphDB, project_id: str):
        nodes = [_node(f"src.mod.n{i}") for i in range(4)]
        edges = [
            _edge("src.mod.n1", "n0"),
            _edge("src.mod.n2", "n1"),
            _edge("src.mod.n3", "n2"),
        ]
        await _seed(db, project_id, nodes, edges)

        result = await db.get_impact_radius("n0", depth=3, project_id=project_id)
        depths = [r["impact_depth"] for r in result]
        assert depths == sorted(depths)

    @pytest.mark.asyncio
    async def test_unknown_function_returns_empty(self, db: CallGraphDB, project_id: str):
        await _seed(db, project_id, [_node("src.mod.fn")])
        result = await db.get_impact_radius("nonexistent", depth=2, project_id=project_id)
        assert result == []
