"""Tests for src/provisioning.py.

Test categories:
  1. Unit tests for generate_password (always run)
  2. Unit tests for _validate_slug and DSN helpers (always run)
  3. Mock tests for provision_org / teardown_org call order (always run)
  4. Integration tests against a real Postgres instance with CREATEDB privilege
     (skipped unless PROVISIONER_DSN is set in the environment)
"""
from __future__ import annotations

import asyncio
import os
import string
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call
import pytest

from src.provisioning import (
    generate_password,
    provision_org,
    teardown_org,
    _validate_slug,
    _replace_dbname,
    _replace_user_and_dbname,
    _split_sql,
)

# ── Environment probes ────────────────────────────────────────────────────────

PROVISIONER_DSN = os.getenv("PROVISIONER_DSN", "")
CONTROL_DSN = os.getenv(
    "CONTROL_DSN",
    "postgresql://phronosis:phronosis@172.21.0.1/phronosis_test",
)

requires_provisioner = pytest.mark.skipif(
    not PROVISIONER_DSN,
    reason="PROVISIONER_DSN not set — skipping integration tests",
)


# ── 1. generate_password ──────────────────────────────────────────────────────

class TestGeneratePassword:
    def test_default_length(self):
        pw = generate_password()
        assert len(pw) == 32

    def test_custom_length(self):
        for n in (8, 16, 64):
            assert len(generate_password(n)) == n

    def test_character_set(self):
        valid = set(string.ascii_letters + string.digits)
        for _ in range(50):
            pw = generate_password(64)
            assert all(c in valid for c in pw), f"Unexpected character in: {pw}"

    def test_uniqueness(self):
        """Two calls should virtually never produce the same result."""
        passwords = {generate_password() for _ in range(100)}
        assert len(passwords) == 100


# ── 2. _validate_slug ─────────────────────────────────────────────────────────

class TestValidateSlug:
    def test_valid_slugs(self):
        for slug in ("acme", "my_org", "org123", "a", "z9"):
            _validate_slug(slug)  # must not raise

    def test_invalid_slug_uppercase(self):
        with pytest.raises(ValueError):
            _validate_slug("ACME")

    def test_invalid_slug_hyphen(self):
        with pytest.raises(ValueError):
            _validate_slug("my-org")

    def test_invalid_slug_leading_underscore(self):
        with pytest.raises(ValueError):
            _validate_slug("_org")

    def test_invalid_slug_empty(self):
        with pytest.raises(ValueError):
            _validate_slug("")

    def test_invalid_slug_too_long(self):
        with pytest.raises(ValueError):
            _validate_slug("a" * 64)


# ── 3. DSN helpers ────────────────────────────────────────────────────────────

class TestDsnHelpers:
    def test_replace_dbname(self):
        result = _replace_dbname("postgresql://user:pw@host:5432/olddb", "newdb")
        assert result == "postgresql://user:pw@host:5432/newdb"

    def test_replace_user_and_dbname(self):
        result = _replace_user_and_dbname(
            "postgresql://prov:secret@myhost:5432/postgres",
            "org_acme_rw",
            "abc123",
            "org_acme",
        )
        assert "org_acme_rw" in result
        assert "abc123" in result
        assert "org_acme" in result
        assert "myhost" in result


# ── 4. _split_sql ─────────────────────────────────────────────────────────────

class TestSplitSql:
    def test_simple_statements(self):
        sql = "CREATE TABLE a (id INT); CREATE TABLE b (id INT);"
        parts = _split_sql(sql)
        assert len(parts) == 2
        assert "CREATE TABLE a" in parts[0]
        assert "CREATE TABLE b" in parts[1]

    def test_dollar_quoted_body(self):
        sql = """
        CREATE OR REPLACE FUNCTION foo() RETURNS void AS $$
        BEGIN
            INSERT INTO t VALUES (1); -- semicolon inside $$
        END;
        $$ LANGUAGE plpgsql;
        SELECT 1;
        """
        parts = [p.strip() for p in _split_sql(sql) if p.strip()]
        # The function body semicolons must NOT split the statement
        assert any("CREATE OR REPLACE FUNCTION" in p for p in parts)
        assert any("SELECT 1" in p for p in parts)

    def test_tagged_dollar_quote(self):
        sql = "$tag$hello; world$tag$; SELECT 2;"
        parts = [p.strip() for p in _split_sql(sql) if p.strip()]
        assert any("$tag$hello; world$tag$" in p for p in parts)
        assert any("SELECT 2" in p for p in parts)


# ── 5. Mock tests for provision_org ──────────────────────────────────────────

class TestProvisionOrgMocked:
    """Verify provision_org calls asyncpg in the correct order without a real DB."""

    @pytest.mark.asyncio
    async def test_provision_org_call_order(self):
        mock_prov_conn = AsyncMock()
        mock_org_conn = AsyncMock()

        # asyncpg.connect returns different connections depending on call count
        connect_results = [mock_prov_conn, mock_org_conn]
        connect_call_count = 0

        async def fake_connect(dsn):
            nonlocal connect_call_count
            result = connect_results[connect_call_count]
            connect_call_count += 1
            return result

        with patch("src.provisioning.asyncpg.connect", side_effect=fake_connect), \
             patch("src.provisioning._apply_schema", new_callable=AsyncMock) as mock_schema, \
             patch("src.provisioning._record_org", new_callable=AsyncMock) as mock_record, \
             patch("src.provisioning.generate_password", return_value="testpass123"):

            result = await provision_org(
                slug="acme",
                provisioner_dsn="postgresql://prov:x@host/postgres",
                control_dsn="postgresql://ctrl:x@host/control",
                schema_sql_path="schema_org.sql",
            )

        # DB was created on provisioner connection
        mock_prov_conn.execute.assert_any_call('CREATE DATABASE "org_acme"')
        # Provisioner connection was closed
        mock_prov_conn.close.assert_called_once()

        # Schema was applied to org connection
        mock_schema.assert_called_once()

        # Role was created on org connection
        execute_calls = [str(c) for c in mock_org_conn.execute.call_args_list]
        assert any("CREATE ROLE" in c and "org_acme_rw" in c for c in execute_calls)
        assert any("GRANT CONNECT" in c and "org_acme" in c for c in execute_calls)
        assert any("REVOKE CONNECT" in c for c in execute_calls)

        # Control record was attempted
        mock_record.assert_called_once()

        # Return value has the expected keys
        assert result["db_name"] == "org_acme"
        assert result["role_name"] == "org_acme_rw"
        assert "connection_string" in result

    @pytest.mark.asyncio
    async def test_teardown_org_call_order(self):
        mock_prov_conn = AsyncMock()

        async def fake_connect(dsn):
            return mock_prov_conn

        with patch("src.provisioning.asyncpg.connect", side_effect=fake_connect), \
             patch("src.provisioning._remove_org_record", new_callable=AsyncMock) as mock_rm:

            result = await teardown_org(
                slug="acme",
                provisioner_dsn="postgresql://prov:x@host/postgres",
                control_dsn="postgresql://ctrl:x@host/control",
            )

        calls = [str(c) for c in mock_prov_conn.execute.call_args_list]
        assert any("DROP DATABASE" in c and "org_acme" in c for c in calls)
        assert any("DROP ROLE" in c and "org_acme_rw" in c for c in calls)
        mock_rm.assert_called_once()

        assert result["dropped_db"] == "org_acme"
        assert result["dropped_role"] == "org_acme_rw"

    @pytest.mark.asyncio
    async def test_invalid_slug_raises(self):
        with pytest.raises(ValueError):
            await provision_org(
                slug="INVALID-SLUG!",
                provisioner_dsn="postgresql://x:x@host/postgres",
                control_dsn="postgresql://x:x@host/control",
            )

    @pytest.mark.asyncio
    async def test_teardown_invalid_slug_raises(self):
        with pytest.raises(ValueError):
            await teardown_org(
                slug="bad slug",
                provisioner_dsn="postgresql://x:x@host/postgres",
                control_dsn="postgresql://x:x@host/control",
            )


# ── 6. Integration tests (skipped without PROVISIONER_DSN) ───────────────────

@requires_provisioner
class TestProvisionOrgIntegration:
    """End-to-end integration tests against a real PostgreSQL with CREATEDB."""

    TEST_SLUG = "scopenos_test_org_tmp"

    @pytest.mark.asyncio
    async def test_provision_and_teardown(self):
        # Ensure clean state
        try:
            await teardown_org(
                slug=self.TEST_SLUG,
                provisioner_dsn=PROVISIONER_DSN,
                control_dsn=CONTROL_DSN,
            )
        except Exception:
            pass  # OK if org didn't exist

        result = await provision_org(
            slug=self.TEST_SLUG,
            provisioner_dsn=PROVISIONER_DSN,
            control_dsn=CONTROL_DSN,
        )
        assert result["db_name"] == f"org_{self.TEST_SLUG}"
        assert result["role_name"] == f"org_{self.TEST_SLUG}_rw"
        assert result["connection_string"]

        # Verify the database exists and is reachable with the new role
        import asyncpg
        conn = await asyncpg.connect(result["connection_string"])
        try:
            tables = await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
            table_names = {r["table_name"] for r in tables}
            # schema_org.sql must have been applied
            assert "projects" in table_names or "users" in table_names, \
                f"Expected schema tables, got: {table_names}"
        finally:
            await conn.close()

        # Teardown
        td = await teardown_org(
            slug=self.TEST_SLUG,
            provisioner_dsn=PROVISIONER_DSN,
            control_dsn=CONTROL_DSN,
        )
        assert td["dropped_db"] == f"org_{self.TEST_SLUG}"
        assert td["dropped_role"] == f"org_{self.TEST_SLUG}_rw"

        # Database must no longer exist
        import asyncpg
        with pytest.raises(Exception):
            conn2 = await asyncpg.connect(result["connection_string"])
            await conn2.close()
