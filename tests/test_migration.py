"""Integration tests for scripts/migrate_to_schemas.py."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.migrate_to_schemas import migrate, PER_PROJECT_TABLES, JOIN_VIA_DECISIONS
from src.call_graph.storage import derive_schema_name

# DSN used by all tests in this module.  Falls back to the default test DSN.
_TEST_DB_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql://scopenos:scopenos@localhost/scopenos_test",
)


def _project_row(project_id: str, schema_name: str = "") -> tuple:
    """Build an INSERT values tuple for the projects table."""
    now = datetime.now(timezone.utc).isoformat()
    return (project_id, f"Test {project_id}", "/tmp", "", "", schema_name, now, now)


_INSERT_PROJECT = """
    INSERT INTO projects(id, name, root, branch, head_commit,
                         schema_name, created_at, last_indexed)
    VALUES($1, $2, $3, $4, $5, $6, $7, $8)
    ON CONFLICT(id) DO UPDATE SET
        name = EXCLUDED.name,
        schema_name = EXCLUDED.schema_name
"""

_INSERT_NODE = """
    INSERT INTO nodes(
        project_id, id, file, module, type, name, signature,
        docstring, body, body_hash, decorators, is_external,
        start_line, end_line, return_type, is_async,
        parameter_names, enclosing_class, structural_layer)
    VALUES($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12,
           $13, $14, $15, $16, $17, $18, $19)
    ON CONFLICT DO NOTHING
"""


def _node_values(project_id: str, fn_id: str) -> tuple:
    return (
        project_id, fn_id,
        "test.py", "test", "function", "fn", "fn()",
        "",       # docstring
        "pass",   # body
        fn_id,    # body_hash (unique per fn)
        "[]",     # decorators
        0,        # is_external
        1, 3,     # start_line, end_line
        "",       # return_type
        0,        # is_async
        "[]",     # parameter_names
        "",       # enclosing_class
        "tree-sitter",
    )


class TestMigrateToSchemas:
    """Integration tests for the migrate_to_schemas migration script."""

    @pytest.mark.asyncio
    async def test_dry_run_does_not_create_schemas(self, db, project_id: str):
        """Dry run must not create any project schemas."""
        # Insert two projects (upsert_project creates their schemas)
        await db.upsert_project("proj_a", "Project A", "/tmp/a")
        await db.upsert_project("proj_b", "Project B", "/tmp/b")

        # Capture schema set AFTER upsert_project (which legitimately creates schemas)
        async with db._pool.acquire() as conn:
            before = {
                r["schema_name"]
                for r in await conn.fetch(
                    """
                    SELECT schema_name FROM information_schema.schemata
                    WHERE schema_name NOT IN
                        ('public','pg_catalog','information_schema','pg_toast')
                      AND schema_name NOT LIKE 'pg_%'
                    """
                )
            }

        # Dry run — must not raise and must not create new schemas
        await migrate(_TEST_DB_URL, dry_run=True)

        async with db._pool.acquire() as conn:
            after = {
                r["schema_name"]
                for r in await conn.fetch(
                    """
                    SELECT schema_name FROM information_schema.schemata
                    WHERE schema_name NOT IN
                        ('public','pg_catalog','information_schema','pg_toast')
                      AND schema_name NOT LIKE 'pg_%'
                    """
                )
            }

        # Only check that dry run didn't ADD new schemas (other schemas from
        # parallel test state don't matter for this invariant)
        new_schemas = after - before
        assert not new_schemas, (
            f"Dry run created new schemas (should be a no-op): {new_schemas}"
        )

    @pytest.mark.asyncio
    async def test_migrate_moves_nodes_to_project_schema(self, db, project_id: str):
        """Execute migration copies nodes from public.nodes to the project schema."""
        project_id = "mig_core_test"
        schema = derive_schema_name(project_id)

        async with db._pool.acquire() as conn:
            # Insert project with schema_name set (migration derives it anyway)
            await conn.execute(_INSERT_PROJECT, *_project_row(project_id, ""))
            # Write a node directly into public.nodes (pre-migration state)
            await conn.execute(_INSERT_NODE, *_node_values(project_id, f"{project_id}.fn"))

        # Confirm the node is in public.nodes and not yet in the project schema
        async with db._pool.acquire() as conn:
            public_count = await conn.fetchval(
                "SELECT COUNT(*) FROM public.nodes WHERE project_id = $1", project_id
            )
        assert public_count == 1, "Node should be in public.nodes before migration"

        # Run the real migration
        await migrate(_TEST_DB_URL, dry_run=False)

        # Verify the node was copied into the project schema
        async with db._pool.acquire() as conn:
            row = await conn.fetchrow(
                f'SELECT id FROM "{schema}".nodes WHERE id = $1',
                f"{project_id}.fn",
            )
        assert row is not None, (
            f"Node should be present in {schema}.nodes after migration"
        )

        # Verify projects.schema_name was updated
        async with db._pool.acquire() as conn:
            proj_row = await conn.fetchrow(
                "SELECT schema_name FROM projects WHERE id = $1", project_id
            )
        assert proj_row is not None, "Project row should still exist after migration"
        assert proj_row["schema_name"] == schema, (
            f"Expected schema_name={schema!r}, got {proj_row['schema_name']!r}"
        )

    @pytest.mark.asyncio
    async def test_migrate_skips_tables_without_project_id(self, db, project_id: str):
        """Migration completes cleanly even if some per-project tables lack rows."""
        project_id = "mig_skip_test"

        async with db._pool.acquire() as conn:
            await conn.execute(_INSERT_PROJECT, *_project_row(project_id, ""))

        # Should complete without error even with no data in per-project tables
        await migrate(_TEST_DB_URL, dry_run=False)

    @pytest.mark.asyncio
    async def test_per_project_tables_constant_matches_schema(self, db, project_id: str):
        """Every table in PER_PROJECT_TABLES should exist in the public schema."""
        async with db._pool.acquire() as conn:
            existing = {
                r["table_name"]
                for r in await conn.fetch(
                    """
                    SELECT table_name FROM information_schema.tables
                    WHERE table_schema = 'public'
                    """
                )
            }

        missing = [t for t in PER_PROJECT_TABLES if t not in existing]
        assert not missing, (
            f"Tables in PER_PROJECT_TABLES not found in public schema: {missing}"
        )

    @pytest.mark.asyncio
    async def test_dry_run_with_no_projects(self, db, project_id: str):
        """Dry run on an empty projects table completes without error."""
        await migrate(_TEST_DB_URL, dry_run=True)  # must not raise

    @pytest.mark.asyncio
    async def test_execute_with_no_projects(self, db, project_id: str):
        """Execute migration with no projects completes cleanly."""
        await migrate(_TEST_DB_URL, dry_run=False)  # must not raise

    @pytest.mark.asyncio
    async def test_migrate_is_idempotent(self, db, project_id: str):
        """Running migration twice should not fail or duplicate data."""
        project_id = "mig_idem_test"
        schema = derive_schema_name(project_id)

        async with db._pool.acquire() as conn:
            await conn.execute(_INSERT_PROJECT, *_project_row(project_id, ""))
            await conn.execute(_INSERT_NODE, *_node_values(project_id, f"{project_id}.fn"))

        # First migration
        await migrate(_TEST_DB_URL, dry_run=False)

        # Verify schema was created and node migrated
        async with db._pool.acquire() as conn:
            count_after_first = await conn.fetchval(
                f'SELECT COUNT(*) FROM "{schema}".nodes WHERE project_id = $1',
                project_id,
            )
        assert count_after_first == 1

        # Second migration — ON CONFLICT DO NOTHING prevents duplicates
        await migrate(_TEST_DB_URL, dry_run=False)

        async with db._pool.acquire() as conn:
            count_after_second = await conn.fetchval(
                f'SELECT COUNT(*) FROM "{schema}".nodes WHERE project_id = $1',
                project_id,
            )
        assert count_after_second == 1, (
            f"Expected exactly 1 node after two migrations, got {count_after_second}"
        )

    @pytest.mark.asyncio
    async def test_migrate_multiple_projects(self, db, project_id: str):
        """Migration handles multiple projects independently."""
        projects = ["mig_multi_a", "mig_multi_b", "mig_multi_c"]

        async with db._pool.acquire() as conn:
            for pid in projects:
                # Use derived schema_name so each row is unique (UNIQUE constraint)
                await conn.execute(_INSERT_PROJECT, *_project_row(pid, derive_schema_name(pid)))
                await conn.execute(_INSERT_NODE, *_node_values(pid, f"{pid}.fn"))

        await migrate(_TEST_DB_URL, dry_run=False)

        # Each project schema should have its node
        async with db._pool.acquire() as conn:
            for pid in projects:
                schema = derive_schema_name(pid)
                row = await conn.fetchrow(
                    f'SELECT id FROM "{schema}".nodes WHERE id = $1',
                    f"{pid}.fn",
                )
                assert row is not None, (
                    f"Node for project {pid} should be in {schema}.nodes"
                )

    @pytest.mark.asyncio
    async def test_join_via_decisions_constant_matches_schema(self, db, project_id: str):
        """Every table in JOIN_VIA_DECISIONS should exist in the public schema."""
        async with db._pool.acquire() as conn:
            existing = {
                r["table_name"]
                for r in await conn.fetch(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            }
        missing = [t for t in JOIN_VIA_DECISIONS if t not in existing]
        assert not missing, (
            f"Tables in JOIN_VIA_DECISIONS not found in public schema: {missing}"
        )

    @pytest.mark.asyncio
    async def test_migrate_decision_embeddings_and_functions(self, db, project_id: str):
        """decision_embeddings and decision_functions are migrated via JOIN through decisions."""
        import uuid
        pid = "mig_decisions_test"
        schema = derive_schema_name(pid)
        decision_id = str(uuid.uuid4())
        fn_id = f"{pid}.some_fn"
        fake_embedding = [0.0] * 1536

        async with db._pool.acquire() as conn:
            await conn.execute(_INSERT_PROJECT, *_project_row(pid, ""))
            # Write decision to public.decisions
            await conn.execute(
                "INSERT INTO decisions(id, project_id, type, description, created_at) "
                "VALUES($1, $2, 'Design', 'test decision', $3) ON CONFLICT DO NOTHING",
                decision_id, pid, "2026-01-01T00:00:00+00:00",
            )
            # Write embedding to public.decision_embeddings (no project_id column)
            await conn.execute(
                "INSERT INTO decision_embeddings(id, embedding) VALUES($1, $2) "
                "ON CONFLICT DO NOTHING",
                decision_id, fake_embedding,
            )
            # Write join row to public.decision_functions (no project_id column)
            await conn.execute(
                "INSERT INTO decision_functions(decision_id, function_id) VALUES($1, $2) "
                "ON CONFLICT DO NOTHING",
                decision_id, fn_id,
            )

        await migrate(_TEST_DB_URL, dry_run=False)

        async with db._pool.acquire() as conn:
            dec_row = await conn.fetchrow(
                f'SELECT id FROM "{schema}".decisions WHERE id = $1', decision_id
            )
            emb_row = await conn.fetchrow(
                f'SELECT id FROM "{schema}".decision_embeddings WHERE id = $1', decision_id
            )
            fn_row = await conn.fetchrow(
                f'SELECT decision_id FROM "{schema}".decision_functions WHERE decision_id = $1',
                decision_id,
            )

        assert dec_row is not None, "decision should be in project schema"
        assert emb_row is not None, "decision_embedding should be in project schema (via JOIN)"
        assert fn_row is not None, "decision_function should be in project schema (via JOIN)"

    @pytest.mark.asyncio
    async def test_decisions_only_flag(self, db, project_id: str):
        """--decisions-only migrates only the three decision tables."""
        import uuid
        pid = "mig_deconly_test"
        schema = derive_schema_name(pid)
        decision_id = str(uuid.uuid4())

        async with db._pool.acquire() as conn:
            await conn.execute(_INSERT_PROJECT, *_project_row(pid, ""))
            # Write a node to public (should NOT be migrated in decisions-only mode)
            await conn.execute(_INSERT_NODE, *_node_values(pid, f"{pid}.fn"))
            # Write a decision to public
            await conn.execute(
                "INSERT INTO decisions(id, project_id, type, description, created_at) "
                "VALUES($1, $2, 'Design', 'test', $3) ON CONFLICT DO NOTHING",
                decision_id, pid, "2026-01-01T00:00:00+00:00",
            )

        await migrate(_TEST_DB_URL, dry_run=False, decisions_only=True)

        async with db._pool.acquire() as conn:
            # Decision should be migrated
            dec_row = await conn.fetchrow(
                f'SELECT id FROM "{schema}".decisions WHERE id = $1', decision_id
            )
            # Node should NOT be migrated (decisions-only skips nodes)
            node_count = await conn.fetchval(
                f'SELECT COUNT(*) FROM "{schema}".nodes WHERE project_id = $1', pid
            )

        assert dec_row is not None, "decision should be migrated in decisions-only mode"
        assert node_count == 0, "nodes should not be migrated in decisions-only mode"
