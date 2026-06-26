#!/usr/bin/env bash
# Create Postgres roles for session-level access control in Scopenos.
#
# Run ONCE at cluster setup as a PostgreSQL superuser.
# Idempotent — uses DO $$ IF NOT EXISTS $$ guards so it is safe to re-run.
#
# Roles created:
#   scopenos_provisioner  — CREATEDB, no login. Used to provision org databases.
#   scopenos_control_rw   — LOGIN, read/write on the scopenos_control database.
#   scopenos_demos_writer — LOGIN, read/write on the demos database.
#   scopenos_demos_reader — LOGIN, read-only on the demos database.
#   scopenos_test_runner  — LOGIN, read/write on scopenos_test, read on demos.
#
# Usage:
#   CONTROL_RW_PASSWORD=... DEMOS_WRITER_PASSWORD=... \
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

: "${CONTROL_RW_PASSWORD:?must set CONTROL_RW_PASSWORD}"
: "${DEMOS_WRITER_PASSWORD:?must set DEMOS_WRITER_PASSWORD}"
: "${DEMOS_READER_PASSWORD:?must set DEMOS_READER_PASSWORD}"
: "${TEST_RUNNER_PASSWORD:?must set TEST_RUNNER_PASSWORD}"

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -v ON_ERROR_STOP=1)

echo "[roles] Creating Scopenos access-control roles..."

# ── Step 1: create roles without passwords (idempotent DO block) ────────────
"${PSQL[@]}" <<'SQL'
DO $$
BEGIN
    -- scopenos_provisioner: CREATEDB, no login (used by provisioning code)
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_provisioner') THEN
        CREATE ROLE scopenos_provisioner CREATEDB NOLOGIN;
        RAISE NOTICE 'Created role: scopenos_provisioner';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_provisioner';
    END IF;

    -- scopenos_control_rw: login role for control-plane DB read/write
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_control_rw') THEN
        CREATE ROLE scopenos_control_rw LOGIN;
        RAISE NOTICE 'Created role: scopenos_control_rw';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_control_rw';
    END IF;

    -- scopenos_demos_writer: login role for demos DB read/write
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_demos_writer') THEN
        CREATE ROLE scopenos_demos_writer LOGIN;
        RAISE NOTICE 'Created role: scopenos_demos_writer';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_demos_writer';
    END IF;

    -- scopenos_demos_reader: login role for demos DB read-only access
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_demos_reader') THEN
        CREATE ROLE scopenos_demos_reader LOGIN;
        RAISE NOTICE 'Created role: scopenos_demos_reader';
    ELSE
        RAISE NOTICE 'Role already exists: scopenos_demos_reader';
    END IF;

    -- scopenos_test_runner: login role for test DB read/write + demos read
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
"${PSQL[@]}" -c "ALTER ROLE scopenos_control_rw  PASSWORD '${CONTROL_RW_PASSWORD}';"
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
echo "  scopenos_provisioner  — CREATEDB NOLOGIN (used by provision_org.py)"
echo "  scopenos_control_rw   — LOGIN, password set (GRANT on control DB via grant_db_access.sh)"
echo "  scopenos_demos_writer — LOGIN, password set (GRANT on demos DB via grant_db_access.sh)"
echo "  scopenos_demos_reader — LOGIN, password set (GRANT on demos DB via grant_db_access.sh)"
echo "  scopenos_test_runner  — LOGIN, password set (GRANT on test DB via grant_db_access.sh)"
echo ""
echo "Next: run grant_db_access.sh for each database to assign the correct roles."
echo "  bash scripts/grant_db_access.sh control"
echo "  bash scripts/grant_db_access.sh demos"
echo "  bash scripts/grant_db_access.sh test"
