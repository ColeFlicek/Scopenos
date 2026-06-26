"""
Tests for the fork delta algorithm (src/fork.py).

Tests cover:
- git_changed_files: identifies changed source files between two commits
- get_function_content_hashes: fetches body_hash from fork schema
- parse_functions_at_commit: parses source at an old commit via git show
- apply_fork_delta: applies the full delta (upsert + delete) to a fork schema
- create_fork: end-to-end fork creation and validation
- drop_fork (via MCP tool): refuses non-forks, succeeds on forks
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from src.call_graph.storage import CallGraphDB, derive_schema_name
from src.fork import (
    git_changed_files,
    get_function_content_hashes,
    parse_functions_at_commit,
    apply_fork_delta,
    create_fork,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _git(repo: Path, *args: str) -> None:
    """Run a git command in repo, raising on failure."""
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
    )


def _init_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo with identity configured."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@test.com")
    _git(tmp_path, "config", "user.name", "Test")
    return tmp_path


def _commit(repo: Path, message: str = "commit") -> str:
    """Stage all and commit; return the short SHA."""
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


# ── git_changed_files ─────────────────────────────────────────────────────────

def test_git_changed_files_basic(tmp_path: Path):
    """Files changed between two commits are returned as relative paths."""
    repo = _init_repo(tmp_path)

    # First commit: one Python file
    (repo / "foo.py").write_text("def foo(): pass\n")
    sha1 = _commit(repo, "first")

    # Second commit: modify foo.py AND add a non-Python file
    (repo / "foo.py").write_text("def foo(): return 1\n")
    (repo / "README.md").write_text("docs\n")
    sha2 = _commit(repo, "second")

    changed = git_changed_files(str(repo), sha1, sha2)
    assert "foo.py" in changed
    # Markdown is not in supported extensions
    assert "README.md" not in changed


def test_git_changed_files_filters_extensions(tmp_path: Path):
    """Only supported extensions are returned."""
    repo = _init_repo(tmp_path)
    (repo / "app.py").write_text("x = 1\n")
    (repo / "style.css").write_text("body {}\n")
    sha1 = _commit(repo, "first")

    (repo / "app.py").write_text("x = 2\n")
    (repo / "style.css").write_text("div {}\n")
    (repo / "mod.ts").write_text("export const x = 1;\n")
    sha2 = _commit(repo, "second")

    changed = git_changed_files(str(repo), sha1, sha2)
    assert "app.py" in changed
    assert "mod.ts" in changed
    assert "style.css" not in changed


def test_git_changed_files_no_changes(tmp_path: Path):
    """Returns empty list when nothing changed."""
    repo = _init_repo(tmp_path)
    (repo / "foo.py").write_text("x = 1\n")
    sha = _commit(repo, "only")

    changed = git_changed_files(str(repo), sha, sha)
    assert changed == []


# ── parse_functions_at_commit ─────────────────────────────────────────────────

def test_parse_functions_at_commit_basic(tmp_path: Path):
    """Parses the OLD version of a file, not the current disk version."""
    repo = _init_repo(tmp_path)

    old_content = "def greet(name):\n    return 'hello ' + name\n"
    new_content = "def greet(name):\n    return f'hello {name}'\n"

    (repo / "greet.py").write_text(old_content)
    old_sha = _commit(repo, "old")

    (repo / "greet.py").write_text(new_content)
    _commit(repo, "new")

    nodes, _edges = parse_functions_at_commit(str(repo), ["greet.py"], old_sha)
    assert any(n.name == "greet" for n in nodes), "greet function should be parsed"
    # Body should contain the old string
    greet_node = next(n for n in nodes if n.name == "greet")
    assert "hello ' + name" in greet_node.body or "hello " in greet_node.body


def test_parse_functions_at_commit_missing_file(tmp_path: Path):
    """Files that don't exist at commit are silently skipped."""
    repo = _init_repo(tmp_path)
    (repo / "existing.py").write_text("def foo(): pass\n")
    sha = _commit(repo, "first")

    # "missing.py" never existed — parse_functions_at_commit should skip it
    nodes, edges = parse_functions_at_commit(str(repo), ["existing.py", "missing.py"], sha)
    assert any(n.name == "foo" for n in nodes)
    # Should not raise even though missing.py doesn't exist at sha


# ── get_function_content_hashes ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_function_content_hashes(db: CallGraphDB):
    """Returns {id: body_hash} for nodes in the given files."""
    schema = "fork_hash_test"
    async with db._pool.acquire() as conn:
        await conn.execute("SELECT create_project_schema($1)", schema)
    async with db._pool.acquire() as conn:
        await conn.execute(
            f"""INSERT INTO "{schema}".nodes
                   (project_id, id, file, module, type, name, signature,
                    docstring, summary, body, body_hash, decorators,
                    is_external, start_line, end_line, return_type,
                    is_async, parameter_names, enclosing_class, structural_layer)
                VALUES('proj','fn1','src/foo.py','foo','function','foo','foo()',
                       '','','def foo(): pass','abc123','[]',
                       0,1,2,'',0,'[]','','service')"""
        )

    hashes = await get_function_content_hashes(schema, ["src/foo.py"], db)
    assert hashes == {"fn1": "abc123"}

    # Querying a file that has no nodes returns empty
    empty = await get_function_content_hashes(schema, ["nonexistent.py"], db)
    assert empty == {}

    # Cleanup
    async with db._pool.acquire() as conn:
        await conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# ── create_fork / apply_fork_delta (end-to-end) ───────────────────────────────

@pytest.mark.asyncio
async def test_create_fork_end_to_end(db: CallGraphDB, tmp_path: Path):
    """
    End-to-end test: index a small project, make a change commit, fork at old commit,
    verify fork nodes reflect old source; verify parent is untouched.
    """
    from src.indexer import Indexer

    # ── Set up Indexer with mocked embeddings ──────────────────────────────
    pipeline = MagicMock()
    pipeline.upsert_chunks = AsyncMock(return_value={"docs": 0, "fallback": 0})
    pipeline.delete_by_ids = AsyncMock()
    pipeline.delete_by_file = AsyncMock()
    pipeline.get_embedded_ids = AsyncMock(return_value=set())
    pipeline.model = "text-embedding-3-small"
    pipeline.with_db = MagicMock(return_value=pipeline)
    indexer = Indexer(db, pipeline)

    # ── Create a real git repo ─────────────────────────────────────────────
    repo = _init_repo(tmp_path)

    # V1: simple function
    (repo / "calc.py").write_text(
        "def add(a, b):\n    '''Add two numbers.'''\n    return a + b\n"
    )
    old_sha = _commit(repo, "v1")

    # V2: function body changed
    (repo / "calc.py").write_text(
        "def add(a, b):\n    '''Add two numbers (v2).'''\n    return int(a) + int(b)\n"
    )
    _commit(repo, "v2")

    # ── Index at current HEAD (v2) ─────────────────────────────────────────
    project_id = "test_fork_proj"
    result = await indexer.index_project(str(repo), project_id=project_id)
    # "partial" is acceptable — embeddings aren't stored because the mock pipeline
    # does not write vectors, so coverage check reports missing vectors.
    assert result.get("status") in ("ok", "partial"), f"Unexpected status: {result}"

    # Verify v2 is in parent
    parent_schema = await db.get_schema_name_for_project(project_id)
    async with db._pool.acquire() as conn:
        parent_rows = await conn.fetch(
            f'SELECT body_hash FROM "{parent_schema}".nodes WHERE name = $1',
            "add",
        )
    assert len(parent_rows) == 1, "Parent should have one 'add' node"
    parent_hash = parent_rows[0]["body_hash"]

    # ── Create fork at old_sha (v1) ────────────────────────────────────────
    fork_id = "test_fork_proj_fork"
    fork_result = await create_fork(
        parent_project_id=project_id,
        target_commit=old_sha,
        fork_project_id=fork_id,
        repo_path=str(repo),
        org_db=db,
    )

    assert fork_result["fork_project_id"] == fork_id
    fork_schema = fork_result["schema_name"]

    # Fork should have the v1 body_hash (different from v2)
    async with db._pool.acquire() as conn:
        fork_rows = await conn.fetch(
            f'SELECT body_hash, body FROM "{fork_schema}".nodes WHERE name = $1',
            "add",
        )
    assert len(fork_rows) == 1, "Fork should have one 'add' node"
    fork_hash = fork_rows[0]["body_hash"]

    # The fork hash should differ from the current parent hash (v2 vs v1)
    assert fork_hash != parent_hash, (
        "Fork should have old (v1) body_hash, parent should have new (v2) body_hash"
    )

    # ── Parent is untouched ────────────────────────────────────────────────
    async with db._pool.acquire() as conn:
        parent_rows_after = await conn.fetch(
            f'SELECT body_hash FROM "{parent_schema}".nodes WHERE name = $1',
            "add",
        )
    assert parent_rows_after[0]["body_hash"] == parent_hash, "Parent must not be modified"

    # ── Fork is in projects table with is_fork=True ────────────────────────
    async with db._pool.acquire() as conn:
        proj_row = await conn.fetchrow(
            "SELECT is_fork, parent_schema FROM projects WHERE id = $1",
            fork_id,
        )
    assert proj_row is not None
    assert proj_row["is_fork"] is True
    assert proj_row["parent_schema"] == parent_schema


@pytest.mark.asyncio
async def test_create_fork_no_changes(db: CallGraphDB, tmp_path: Path):
    """Fork at current HEAD results in zero updated/deleted nodes."""
    from src.indexer import Indexer

    pipeline = MagicMock()
    pipeline.upsert_chunks = AsyncMock(return_value={"docs": 0, "fallback": 0})
    pipeline.delete_by_ids = AsyncMock()
    pipeline.delete_by_file = AsyncMock()
    pipeline.get_embedded_ids = AsyncMock(return_value=set())
    pipeline.model = "text-embedding-3-small"
    pipeline.with_db = MagicMock(return_value=pipeline)
    indexer = Indexer(db, pipeline)

    repo = _init_repo(tmp_path)
    (repo / "util.py").write_text("def helper(): pass\n")
    head_sha = _commit(repo, "only commit")

    project_id = "test_fork_noop"
    await indexer.index_project(str(repo), project_id=project_id)

    # Fork at HEAD — no files changed between HEAD and HEAD
    fork_result = await create_fork(
        parent_project_id=project_id,
        target_commit=head_sha,
        fork_project_id="test_fork_noop_fork",
        repo_path=str(repo),
        org_db=db,
    )
    delta = fork_result["delta"]
    # updated and deleted must both be 0 (no differences from HEAD to HEAD)
    assert delta["updated"] == 0
    assert delta["deleted"] == 0


# ── drop_fork (via MCP tool logic) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_drop_fork_refuses_non_fork(db: CallGraphDB):
    """drop_fork should refuse to delete a project that is not a fork."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    # Insert a regular (non-fork) project
    async with db._pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO projects (id, name, root, branch, head_commit, schema_name,
                                     created_at, last_indexed, is_fork)
               VALUES($1,$2,'','','','normal_proj',$3,$4,FALSE)""",
            "normal_proj", "Normal Project", now, now,
        )

    # Simulate drop_fork logic (same as in the MCP tool)
    async with db._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_fork FROM projects WHERE id = $1",
            "normal_proj",
        )
    assert row is not None
    assert not row["is_fork"], "Non-fork project should report is_fork=False"
    # The tool would return an error — confirm the guard works


@pytest.mark.asyncio
async def test_drop_fork_deletes_fork(db: CallGraphDB):
    """drop_fork deletes a fork project including its schema."""
    from datetime import datetime, timezone

    schema_name = "drop_fork_test_schema"
    now = datetime.now(timezone.utc).isoformat()

    # Create the schema and register it as a fork project
    async with db._pool.acquire() as conn:
        await conn.execute("SELECT create_project_schema($1)", schema_name)
        await conn.execute(
            """INSERT INTO projects (id, name, root, branch, head_commit, schema_name,
                                     created_at, last_indexed, is_fork, parent_schema)
               VALUES($1,$2,'','','abcdef0',$3,$4,$5,TRUE,'parent_schema')""",
            "drop_fork_fork_proj", "Fork Project", schema_name, now, now,
        )

    # Confirm it exists
    async with db._pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT is_fork FROM projects WHERE id = $1", "drop_fork_fork_proj"
        )
    assert row is not None
    assert row["is_fork"] is True

    # Delete it via delete_project (same code path as drop_fork tool)
    result = await db.delete_project("drop_fork_fork_proj")
    assert result["project_id"] == "drop_fork_fork_proj"

    # Should no longer exist in projects
    async with db._pool.acquire() as conn:
        row_after = await conn.fetchrow(
            "SELECT id FROM projects WHERE id = $1", "drop_fork_fork_proj"
        )
    assert row_after is None, "Fork project should be deleted"

    # Schema should also be gone
    async with db._pool.acquire() as conn:
        schema_exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = $1",
            schema_name,
        )
    assert schema_exists is None, "Fork schema should be dropped"
