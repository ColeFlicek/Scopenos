"""Infrastructure smoke tests — deterministic assertions about live system state.

Tests verify that run_checks() detects specific failure modes. Each test either
sets up a broken state and asserts FAIL, or a healthy state and asserts PASS.

All tests require TEST_DATABASE_URL (same as other integration tests).
The control-plane tables (users, api_keys, etc.) must exist in the test DB.
"""
from __future__ import annotations

import pytest
import pytest_asyncio

import hashlib

from src.infra_smoke import run_checks, CheckResult


# ── Check 1: role_can_dml_control_tables ─────────────────────────────────────

@pytest.mark.asyncio
async def test_privilege_check_fails_when_role_does_not_exist(db):
    """A role that doesn't exist cannot have grants — detected as FAIL."""
    async with db._pool.acquire() as conn:
        results = await run_checks(conn, role="smoke_nonexistent_role_xyz")
        check = next(r for r in results if r.name == "role_can_dml_control_tables")
        assert check.status == "FAIL"
        assert "smoke_nonexistent_role_xyz" in check.detail


@pytest.mark.asyncio
async def test_privilege_check_passes_for_role_with_full_grants(db):
    """The current user (who has full access) passes the privilege check."""
    async with db._pool.acquire() as conn:
        current_user = await conn.fetchval("SELECT current_user")
        # Scope to tables that exist in the test DB (not the full control-plane set)
        results = await run_checks(conn, role=current_user, tables=["users", "api_keys"])
        check = next(r for r in results if r.name == "role_can_dml_control_tables")
        assert check.status == "PASS"


# ── Check 2: env_key_resolves ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_env_key_check_fails_when_key_not_in_db(db):
    """An env key whose hash has no matching row is detected as FAIL."""
    async with db._pool.acquire() as conn:
        results = await run_checks(conn, env_key="not-a-real-key-xyz")
        check = next(r for r in results if r.name == "env_key_resolves")
        assert check.status == "FAIL"
        assert "SCOPENOS_API_KEY" in check.detail


@pytest.mark.asyncio
async def test_env_key_check_passes_when_key_exists_in_db(db):
    """An env key whose hash is in api_keys passes."""
    user = await db.create_user("smoke@example.com")
    raw_key = await db.create_api_key(user["id"], name="smoke-test-key")

    async with db._pool.acquire() as conn:
        results = await run_checks(conn, env_key=raw_key)
        check = next(r for r in results if r.name == "env_key_resolves")
        assert check.status == "PASS"
        assert "smoke-test-key" in check.detail


# ── Check 3: metadata_accuracy ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_metadata_check_fails_when_node_count_diverges(db):
    """A project whose node_count doesn't match actual function count is FAIL."""
    async with db._pool.acquire() as conn:
        schema = "smoke_stale_meta"
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'CREATE TABLE "{schema}".functions (id TEXT PRIMARY KEY)')
        await conn.execute(f"INSERT INTO \"{schema}\".functions VALUES ('fn1'), ('fn2')")
        await conn.execute(
            "INSERT INTO projects (id, name, root, schema_name, created_at, node_count) "
            "VALUES ('smoke-meta', 'smoke', '/tmp', $1, '2026-01-01', 99)",
            schema,
        )

        results = await run_checks(conn, org_conn=conn)
        check = next(r for r in results if r.name == "metadata_accuracy")
        assert check.status == "FAIL"
        assert "smoke-meta" in check.detail


@pytest.mark.asyncio
async def test_metadata_check_passes_when_count_matches(db):
    """A project whose node_count matches actual function count passes."""
    async with db._pool.acquire() as conn:
        schema = "smoke_fresh_meta"
        await conn.execute(f'CREATE SCHEMA "{schema}"')
        await conn.execute(f'CREATE TABLE "{schema}".functions (id TEXT PRIMARY KEY)')
        await conn.execute(f"INSERT INTO \"{schema}\".functions VALUES ('fn1'), ('fn2')")
        await conn.execute(
            "INSERT INTO projects (id, name, root, schema_name, created_at, node_count) "
            "VALUES ('smoke-meta-ok', 'smoke-ok', '/tmp', $1, '2026-01-01', 2)",
            schema,
        )

        results = await run_checks(conn, org_conn=conn)
        check = next(r for r in results if r.name == "metadata_accuracy")
        assert check.status == "PASS"
