"""Multi-user isolation integration tests.

Verifies that two users with separate projects cannot see each other's data,
even when using the same project name. Each user's project lives in its own
Postgres schema.
"""
from __future__ import annotations

import pytest

from src.call_graph.parser import FunctionNode


def _make_node(node_id: str, name: str) -> FunctionNode:
    """Build a minimal FunctionNode for testing."""
    return FunctionNode(
        id=node_id,
        file="m.py",
        module="mod",
        type="function",
        name=name,
        signature=f"{name}()",
        docstring="",
        body="pass",
        body_hash=f"hash_{name}",
        decorators=[],
        is_external=False,
        start_line=1,
        end_line=2,
        return_type="",
        is_async=False,
        parameter_names=[],
        enclosing_class="",
        structural_layer="tree-sitter",
    )


class TestSchemaIsolation:
    """Two users, two projects: data must not bleed across schemas."""

    @pytest.mark.asyncio
    async def test_separate_project_ids_get_separate_schemas(self, db):
        """user_a and user_b both name their project 'myapp' but get distinct schemas."""
        await db.upsert_project("user_a_myapp", "myapp", "/a")
        await db.upsert_project("user_b_myapp", "myapp", "/b")

        schema_a = await db.get_schema_name_for_project("user_a_myapp")
        schema_b = await db.get_schema_name_for_project("user_b_myapp")
        assert schema_a != schema_b

    @pytest.mark.asyncio
    async def test_nodes_written_to_correct_schema(self, db):
        """Nodes written for user_a's project do not appear in user_b's project."""
        await db.upsert_project("ua_proj", "proj", "/a")
        await db.upsert_project("ub_proj", "proj", "/b")

        pdb_a = await db.project_db(await db.get_schema_name_for_project("ua_proj"))
        pdb_b = await db.project_db(await db.get_schema_name_for_project("ub_proj"))

        node_a = _make_node("mod.fn_a", "fn_a")
        node_b = _make_node("mod.fn_b", "fn_b")

        await pdb_a.upsert_nodes([node_a], "ua_proj")
        await pdb_b.upsert_nodes([node_b], "ub_proj")

        nodes_a = await pdb_a.get_all_nodes("ua_proj")
        nodes_b = await pdb_b.get_all_nodes("ub_proj")

        a_names = {n["name"] for n in nodes_a}
        b_names = {n["name"] for n in nodes_b}

        assert "fn_a" in a_names
        assert "fn_b" not in a_names  # fn_b must not leak into user_a's view
        assert "fn_b" in b_names
        assert "fn_a" not in b_names

    @pytest.mark.asyncio
    async def test_access_control_prevents_cross_user_reads(self, db):
        """check_project_access returns False for a user who doesn't own the project."""
        # Create users first (project_access FK → users)
        alice = await db.create_user("alice@example.com")
        bob = await db.create_user("bob@example.com")

        await db.upsert_project("isolated_proj", "proj", "/x")
        await db.grant_project_access(alice["id"], "isolated_proj", "owner")

        # Bob has no access
        has_access = await db.check_project_access(bob["id"], "isolated_proj", "read")
        assert not has_access

        alice_access = await db.check_project_access(alice["id"], "isolated_proj", "read")
        assert alice_access

    @pytest.mark.asyncio
    async def test_accessible_projects_scoped_per_user(self, db):
        """list_user_projects returns only projects the requesting user owns."""
        from datetime import datetime, timezone
        alice = await db.create_user("alice@example.com")
        bob = await db.create_user("bob@example.com")

        # Insert project rows directly (no schema creation) to avoid the
        # asyncpg session-pool interaction that wipes users between DDL calls.
        now = datetime.now(timezone.utc).isoformat()
        async with db._pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO projects(id, name, root, schema_name, created_at, last_indexed) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                "alice_proj_1", "alice1", "/a1", "alice_proj_1_schema", now, now,
            )
            await conn.execute(
                "INSERT INTO projects(id, name, root, schema_name, created_at, last_indexed) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                "alice_proj_2", "alice2", "/a2", "alice_proj_2_schema", now, now,
            )
            await conn.execute(
                "INSERT INTO projects(id, name, root, schema_name, created_at, last_indexed) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                "bob_proj_1", "bob1", "/b1", "bob_proj_1_schema", now, now,
            )
            await conn.execute(
                "INSERT INTO project_access (user_id, project_id, role) VALUES ($1,$2,$3)",
                alice["id"], "alice_proj_1", "owner",
            )
            await conn.execute(
                "INSERT INTO project_access (user_id, project_id, role) VALUES ($1,$2,$3)",
                alice["id"], "alice_proj_2", "owner",
            )
            await conn.execute(
                "INSERT INTO project_access (user_id, project_id, role) VALUES ($1,$2,$3)",
                bob["id"], "bob_proj_1", "owner",
            )

        alice_projects = await db.list_user_projects(alice["id"])
        bob_projects = await db.list_user_projects(bob["id"])

        alice_ids = {p["id"] for p in alice_projects}
        bob_ids = {p["id"] for p in bob_projects}

        assert "alice_proj_1" in alice_ids
        assert "alice_proj_2" in alice_ids
        assert "bob_proj_1" not in alice_ids

        assert "bob_proj_1" in bob_ids
        assert "alice_proj_1" not in bob_ids

    @pytest.mark.asyncio
    async def test_global_tables_shared_not_duplicated(self, db):
        """embedding_cache and pattern_prototypes live in public schema — shared across all users."""
        await db.upsert_project("ua_global", "p", "/a")
        await db.upsert_project("ub_global", "p", "/b")

        schema_a = await db.get_schema_name_for_project("ua_global")
        schema_b = await db.get_schema_name_for_project("ub_global")

        # Verify the two projects have distinct schemas
        assert schema_a != schema_b

        # Verify that embedding_cache lives only in the public schema, not in
        # either project schema. This confirms it's a shared global table.
        async with db._pool.acquire() as conn:
            # embedding_cache must NOT exist in either project schema
            in_a = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=$1 AND table_name='embedding_cache')",
                schema_a,
            )
            in_b = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema=$1 AND table_name='embedding_cache')",
                schema_b,
            )
            in_public = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name='embedding_cache')"
            )

        assert not in_a, "embedding_cache must not exist in user_a's project schema"
        assert not in_b, "embedding_cache must not exist in user_b's project schema"
        assert in_public, "embedding_cache must exist in the public (shared) schema"


class TestForkIsolation:
    """Fork isolation: user_a's fork is invisible to user_b."""

    @pytest.mark.asyncio
    async def test_fork_not_visible_to_other_user(self, db):
        """After user_a creates a fork, user_b's accessible projects don't include it."""
        from datetime import datetime, timezone
        alice = await db.create_user("alice@example.com")
        bob = await db.create_user("bob@example.com")

        now = datetime.now(timezone.utc).isoformat()
        async with db._pool.acquire() as conn:
            # Insert base project row (no schema creation needed for this assertion)
            await conn.execute(
                "INSERT INTO projects(id, name, root, schema_name, created_at, last_indexed) "
                "VALUES($1,$2,$3,$4,$5,$6)",
                "alice_base", "base", "/base", "alice_base_schema", now, now,
            )
            await conn.execute(
                "INSERT INTO project_access (user_id, project_id, role) VALUES ($1,$2,$3)",
                alice["id"], "alice_base", "owner",
            )

            # Insert fork project row directly (skip git dependency)
            await conn.execute(
                "INSERT INTO projects(id, name, root, schema_name, created_at, last_indexed, "
                "is_fork, parent_schema) VALUES($1,$2,$3,$4,$5,$6,TRUE,$7)",
                "alice_fork_abc1234", "base@abc1234", "/base",
                "alice_base_schema_fork", now, now, "alice_base_schema",
            )
            await conn.execute(
                "INSERT INTO project_access (user_id, project_id, role) VALUES ($1,$2,$3)",
                alice["id"], "alice_fork_abc1234", "owner",
            )
        # Bob gets NO access to the fork

        bob_projects = await db.list_user_projects(bob["id"])
        bob_ids = {p["id"] for p in bob_projects}
        assert "alice_fork_abc1234" not in bob_ids
        assert "alice_base" not in bob_ids

    @pytest.mark.asyncio
    async def test_drop_fork_leaves_parent_untouched(self, db):
        """Dropping a fork schema must not affect the parent schema's nodes."""
        await db.upsert_project("parent_proj", "parent", "/parent")
        schema_p = await db.get_schema_name_for_project("parent_proj")
        pdb_p = await db.project_db(schema_p)

        # Insert a node into parent
        node = FunctionNode(
            id="mod.fn",
            file="m.py",
            module="mod",
            type="function",
            name="fn",
            signature="fn()",
            docstring="",
            body="return 42",
            body_hash="xyz",
            decorators=[],
            is_external=False,
            start_line=1,
            end_line=2,
            return_type="",
            is_async=False,
            parameter_names=[],
            enclosing_class="",
            structural_layer="tree-sitter",
        )
        await pdb_p.upsert_nodes([node], "parent_proj")

        # Create a fork schema (directly, without git)
        fork_schema = f"{schema_p}_fork_abc1234"[:63]
        await db.fork_schema(schema_p, fork_schema)
        await db.upsert_project("fork_proj", "fork", "/parent")
        async with db._pool.acquire() as conn:
            await conn.execute(
                "UPDATE projects SET is_fork=TRUE, parent_schema=$1, schema_name=$2 WHERE id=$3",
                schema_p,
                fork_schema,
                "fork_proj",
            )

        # Drop the fork
        await db.delete_project("fork_proj")

        # Parent node must still exist
        nodes = await pdb_p.get_all_nodes("parent_proj")

        assert any(n["name"] == "fn" for n in nodes), "Parent node must survive fork deletion"
