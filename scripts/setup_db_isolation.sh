#!/usr/bin/env bash
# Create Postgres roles for session-level access control in Scopenos.
#
# Run ONCE at cluster setup as a PostgreSQL superuser.
# Idempotent — uses DO $$ IF NOT EXISTS $$ guards so it is safe to re-run.
#
# Roles created (one per Claude Code session identity):
#   scopenos_provisioner  — LOGIN, CREATEDB. scopenos-provisioner session.
#   scopenos_control_rw   — LOGIN, read/write production DB. scopenos-indexer session.
#   scopenos_read         — LOGIN, SELECT only on production DB. scopenos-reader session.
#   scopenos_migrator     — LOGIN, CREATE SCHEMA + DDL on schemas. scopenos-migrator session.
#   scopenos_demos_writer — LOGIN, read/write on the demos database.
#   scopenos_demos_reader — LOGIN, read-only on the demos database.
#   scopenos_test_runner  — LOGIN, read/write on scopenos_test, read on demos. scopenos-tester session.
#
# Usage:
#   PROVISIONER_PASSWORD=... CONTROL_RW_PASSWORD=... READ_PASSWORD=... \
#   MIGRATOR_PASSWORD=... DEMOS_WRITER_PASSWORD=... \
#   DEMOS_READER_PASSWORD=... TEST_RUNNER_PASSWORD=... \
#   bash scripts/setup_db_isolation.sh
#
# Optional overrides:
#   PGHOST  (default: localhost)
#   PGPORT  (default: 5432)
#   PGUSER  (default: postgres)

set -euo pipefail

: "${PGHOST:=localhost}"
: "${PGPORT:=5432}"
: "${PGUSER:=postgres}"

: "${PROVISIONER_PASSWORD:?must set PROVISIONER_PASSWORD}"
: "${CONTROL_RW_PASSWORD:?must set CONTROL_RW_PASSWORD}"
: "${READ_PASSWORD:?must set READ_PASSWORD}"
: "${MIGRATOR_PASSWORD:?must set MIGRATOR_PASSWORD}"
: "${DEMOS_WRITER_PASSWORD:?must set DEMOS_WRITER_PASSWORD}"
: "${DEMOS_READER_PASSWORD:?must set DEMOS_READER_PASSWORD}"
: "${TEST_RUNNER_PASSWORD:?must set TEST_RUNNER_PASSWORD}"

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -v ON_ERROR_STOP=1)

echo "[roles] Creating Scopenos access-control roles..."

# ── Step 1: create roles without passwords (idempotent DO block) ────────────
"${PSQL[@]}" <<'SQL'
DO $$
BEGIN
    -- scopenos_provisioner: LOGIN, CREATEDB — used by scopenos-provisioner session
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_provisioner') THEN
        CREATE ROLE scopenos_provisioner CREATEDB LOGIN;
        RAISE NOTICE 'Created role: scopenos_provisioner';
    ELSE
        ALTER ROLE scopenos_provisioner LOGIN;
        RAISE NOTICE 'Role already exists (ensured LOGIN): scopenos_provisioner';
    END IF;

    -- scopenos_control_rw: read/write production DB — scopenos-indexer session
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_control_rw') THEN
        CREATE ROLE scopenos_control_rw LOGIN;
        RAISE NOTICE 'Created role: scopenos_control_rw';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_control_rw';
    END IF;

    -- scopenos_read: SELECT only on all tables — scopenos-reader session
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_read') THEN
        CREATE ROLE scopenos_read LOGIN;
        RAISE NOTICE 'Created role: scopenos_read';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_read';
    END IF;

    -- scopenos_migrator: CREATE SCHEMA + DDL, no DROP DATABASE — scopenos-migrator session
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_migrator') THEN
        CREATE ROLE scopenos_migrator LOGIN;
        RAISE NOTICE 'Created role: scopenos_migrator';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_migrator';
    END IF;

    -- scopenos_demos_writer: read/write on demos database
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_demos_writer') THEN
        CREATE ROLE scopenos_demos_writer LOGIN;
        RAISE NOTICE 'Created role: scopenos_demos_writer';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_demos_writer';
    END IF;

    -- scopenos_demos_reader: read-only on demos database
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_demos_reader') THEN
        CREATE ROLE scopenos_demos_reader LOGIN;
        RAISE NOTICE 'Created role: scopenos_demos_reader';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_demos_reader';
    END IF;

    -- scopenos_test_runner: read/write on test DB + demos read — scopenos-tester session
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_test_runner') THEN
        CREATE ROLE scopenos_test_runner LOGIN;
        RAISE NOTICE 'Created role: scopenos_test_runner';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_test_runner';
    END IF;
END $$;
SQL

# ── Step 2: set passwords (ALTER ROLE cannot run inside DO $$ blocks) ────────
echo "[roles] Setting passwords..."
"${PSQL[@]}" -c "ALTER ROLE scopenos_provisioner  PASSWORD '${PROVISIONER_PASSWORD}';"
"${PSQL[@]}" -c "ALTER ROLE scopenos_control_rw   PASSWORD '${CONTROL_RW_PASSWORD}';"
"${PSQL[@]}" -c "ALTER ROLE scopenos_read         PASSWORD '${READ_PASSWORD}';"
"${PSQL[@]}" -c "ALTER ROLE scopenos_migrator     PASSWORD '${MIGRATOR_PASSWORD}';"
"${PSQL[@]}" -c "ALTER ROLE scopenos_demos_writer PASSWORD '${DEMOS_WRITER_PASSWORD}';"
"${PSQL[@]}" -c "ALTER ROLE scopenos_demos_reader PASSWORD '${DEMOS_READER_PASSWORD}';"
"${PSQL[@]}" -c "ALTER ROLE scopenos_test_runner  PASSWORD '${TEST_RUNNER_PASSWORD}';"

# ── Step 3: role memberships ─────────────────────────────────────────────────
echo "[roles] Configuring role memberships..."
"${PSQL[@]}" <<'SQL'
DO $$
BEGIN
    -- test_runner inherits demos read access
    IF NOT pg_has_role('scopenos_test_runner', 'scopenos_demos_reader', 'member') THEN
        GRANT scopenos_demos_reader TO scopenos_test_runner;
        RAISE NOTICE 'Granted scopenos_demos_reader to scopenos_test_runner';
    ELSE
        RAISE NOTICE 'scopenos_test_runner already has scopenos_demos_reader';
    END IF;
END $$;
SQL

echo ""
echo "[roles] Done. Summary:"
echo "  scopenos_provisioner  — LOGIN CREATEDB (scopenos-provisioner session)"
echo "  scopenos_control_rw   — LOGIN (scopenos-indexer session; GRANT via grant_db_access.sh control)"
echo "  scopenos_read         — LOGIN SELECT only (scopenos-reader session; GRANT via grant_db_access.sh read)"
echo "  scopenos_migrator     — LOGIN DDL (scopenos-migrator session; GRANT via grant_db_access.sh migrator)"
echo "  scopenos_demos_writer — LOGIN (GRANT via grant_db_access.sh demos)"
echo "  scopenos_demos_reader — LOGIN (GRANT via grant_db_access.sh demos)"
echo "  scopenos_test_runner  — LOGIN (scopenos-tester session; GRANT via grant_db_access.sh test)"
echo ""
echo "Next: run grant_db_access.sh for each database to assign the correct privileges."
echo "  bash scripts/grant_db_access.sh control"
echo "  bash scripts/grant_db_access.sh read"
echo "  bash scripts/grant_db_access.sh migrator"
echo "  bash scripts/grant_db_access.sh demos"
echo "  bash scripts/grant_db_access.sh test"
