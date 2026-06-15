"""
TDD regression tests for the Phronosis storage pipeline.

Covers the bugs surfaced during K8s deployment:
- _QueryContext.description was always empty → dict(zip(cols, row)) produced {}
  for every row, causing KeyError: 'id' in reembed_project and similar callers.
- get_all_nodes, get_node, get_nodes_by_file all depend on description being set.

These tests use a real Postgres DB (db fixture from conftest.py).
"""
import pytest
from src.call_graph.parser import FunctionNode


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_node(node_id: str, file: str = "/project/mod.py") -> FunctionNode:
    return FunctionNode(
        id=node_id,
        name=node_id.split(".")[-1],
        file=file,
        module="mod",
        type="function",
        signature=f"def {node_id.split('.')[-1]}():",
        body="pass",
        docstring="",
        body_hash="abc123",
        decorators=[],
        is_external=False,
    )


# ── get_project_root ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_project_root_returns_root_after_upsert(db):
    await db.upsert_project("myapp", "My App", root="/workspace/myapp")
    root = await db.get_project_root("myapp")
    assert root == "/workspace/myapp"


@pytest.mark.asyncio
async def test_get_project_root_returns_empty_for_unknown_project(db):
    root = await db.get_project_root("does-not-exist")
    assert root == ""


# ── get_all_nodes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_nodes_returns_dicts_with_required_keys(db):
    await db.upsert_project("proj", "Proj", root="/src")
    await db.upsert_nodes([_make_node("mod.hello")], project_id="proj")

    nodes = await db.get_all_nodes("proj")

    assert len(nodes) == 1
    node = nodes[0]
    for key in ("id", "name", "file", "module", "type", "signature", "docstring", "summary"):
        assert key in node, f"missing key: {key!r}"


@pytest.mark.asyncio
async def test_get_all_nodes_returns_correct_values(db):
    await db.upsert_project("proj", "Proj", root="/src")
    await db.upsert_nodes([_make_node("mod.hello", file="/src/mod.py")], project_id="proj")

    nodes = await db.get_all_nodes("proj")

    assert nodes[0]["id"] == "mod.hello"
    assert nodes[0]["name"] == "hello"
    assert nodes[0]["file"] == "/src/mod.py"


@pytest.mark.asyncio
async def test_get_all_nodes_empty_for_unknown_project(db):
    nodes = await db.get_all_nodes("no-such-project")
    assert nodes == []


@pytest.mark.asyncio
async def test_get_all_nodes_scoped_to_project(db):
    await db.upsert_project("proj-a", "A", root="/a")
    await db.upsert_project("proj-b", "B", root="/b")
    await db.upsert_nodes([_make_node("mod.fn_a")], project_id="proj-a")
    await db.upsert_nodes([_make_node("mod.fn_b")], project_id="proj-b")

    nodes_a = await db.get_all_nodes("proj-a")
    assert len(nodes_a) == 1
    assert nodes_a[0]["id"] == "mod.fn_a"


# ── get_node ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_node_returns_dict_with_required_keys(db):
    await db.upsert_project("proj", "Proj", root="/src")
    await db.upsert_nodes([_make_node("mod.hello")], project_id="proj")

    node = await db.get_node("mod.hello", project_id="proj")

    assert node is not None
    for key in ("id", "name", "file", "module", "type", "signature"):
        assert key in node, f"missing key: {key!r}"


@pytest.mark.asyncio
async def test_get_node_returns_none_for_missing(db):
    node = await db.get_node("does.not.exist")
    assert node is None


# ── get_nodes_by_file ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_nodes_by_file_returns_only_matching_file(db):
    await db.upsert_project("proj", "Proj", root="/src")
    await db.upsert_nodes([
        _make_node("mod.fn_a", file="/src/mod.py"),
        _make_node("other.fn_b", file="/src/other.py"),
    ], project_id="proj")

    nodes = await db.get_nodes_by_file("/src/mod.py", project_id="proj")

    assert len(nodes) == 1
    assert nodes[0]["id"] == "mod.fn_a"
