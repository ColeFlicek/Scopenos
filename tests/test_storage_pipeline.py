"""
TDD regression tests for the Scopenos storage pipeline.

Covers the bugs surfaced during K8s deployment:
- _QueryContext.description was always empty → dict(zip(cols, row)) produced {}
  for every row, causing KeyError: 'id' in reembed_project and similar callers.
- get_all_nodes, get_node, get_nodes_by_file all depend on description being set.

Uses the `db` fixture (real Postgres, persistent). Each test uses `project_id`
to get its own namespace — no cleanup needed between runs.
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
async def test_get_project_root_returns_root_after_upsert(db, project_id):
    await db.upsert_project(project_id, "My App", root="/workspace/myapp")
    root = await db.get_project_root(project_id)
    assert root == "/workspace/myapp"


@pytest.mark.asyncio
async def test_get_project_root_returns_empty_for_unknown_project(db, project_id):
    root = await db.get_project_root(f"{project_id}_does_not_exist")
    assert root == ""


# ── get_all_nodes ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_all_nodes_returns_dicts_with_required_keys(db, project_id):
    await db.upsert_project(project_id, "Proj", root="/src")
    await db.upsert_nodes([_make_node("mod.hello")], project_id=project_id)

    nodes = await db.get_all_nodes(project_id)

    assert len(nodes) >= 1
    node = next(n for n in nodes if n["id"] == "mod.hello")
    for key in ("id", "name", "file", "module", "type", "signature", "docstring", "summary"):
        assert key in node, f"missing key: {key!r}"


@pytest.mark.asyncio
async def test_get_all_nodes_returns_correct_values(db, project_id):
    await db.upsert_project(project_id, "Proj", root="/src")
    await db.upsert_nodes([_make_node("mod.hello", file="/src/mod.py")], project_id=project_id)

    nodes = await db.get_all_nodes(project_id)
    node = next(n for n in nodes if n["id"] == "mod.hello")

    assert node["id"] == "mod.hello"
    assert node["name"] == "hello"
    assert node["file"] == "/src/mod.py"


@pytest.mark.asyncio
async def test_get_all_nodes_empty_for_unknown_project(db, project_id):
    nodes = await db.get_all_nodes(f"{project_id}_no_such_project")
    assert nodes == []


@pytest.mark.asyncio
async def test_get_all_nodes_scoped_to_project(db, project_id):
    proj_a = f"{project_id}a"
    proj_b = f"{project_id}b"
    await db.upsert_project(proj_a, "A", root="/a")
    await db.upsert_project(proj_b, "B", root="/b")
    await db.upsert_nodes([_make_node("mod.fn_a")], project_id=proj_a)
    await db.upsert_nodes([_make_node("mod.fn_b")], project_id=proj_b)

    nodes_a = await db.get_all_nodes(proj_a)
    assert any(n["id"] == "mod.fn_a" for n in nodes_a)
    assert all(n["id"] != "mod.fn_b" for n in nodes_a)


# ── get_node ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_node_returns_dict_with_required_keys(db, project_id):
    await db.upsert_project(project_id, "Proj", root="/src")
    await db.upsert_nodes([_make_node("mod.hello")], project_id=project_id)

    node = await db.get_node("mod.hello", project_id=project_id)

    assert node is not None
    for key in ("id", "name", "file", "module", "type", "signature"):
        assert key in node, f"missing key: {key!r}"


@pytest.mark.asyncio
async def test_get_node_returns_none_for_missing(db, project_id):
    node = await db.get_node(f"{project_id}.does.not.exist")
    assert node is None


# ── get_nodes_by_file ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_nodes_by_file_returns_only_matching_file(db, project_id):
    await db.upsert_project(project_id, "Proj", root="/src")
    await db.upsert_nodes([
        _make_node("mod.fn_a", file="/src/mod.py"),
        _make_node("other.fn_b", file="/src/other.py"),
    ], project_id=project_id)

    nodes = await db.get_nodes_by_file("/src/mod.py", project_id=project_id)

    assert any(n["id"] == "mod.fn_a" for n in nodes)
    assert all(n["id"] != "other.fn_b" for n in nodes)
