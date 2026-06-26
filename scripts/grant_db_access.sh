#!/usr/bin/env bash
# Grant (or confirm) the correct Scopenos roles on a given database.
#
# Run as a Postgres superuser after setup_db_isolation.sh has created the roles.
# Idempotent — GRANT is safe to re-run.
#
# Usage:
#   bash scripts/grant_db_access.sh {control|demos|test|org_<slug>}
#
# Optional overrides:
#   PGHOST  (default: localhost)
#   PGPORT  (default: 5432)
#   PGUSER  (default: postgres)
#
# DB type → role mapping:
#   control    → scopenos_control_rw  (CONNECT + ALL TABLES)
#   demos      → scopenos_demos_writer (CONNECT + ALL TABLES)
#              → scopenos_demos_reader (CONNECT + SELECT)
#   test       → scopenos_test_runner  (CONNECT + ALL TABLES)
#   org_<slug> → org_<slug>_rw        (CONNECT + ALL TABLES, created by provision_org.py)

set -euo pipefail

: "${PGHOST:=localhost}"
: "${PGPORT:=5432}"
: "${PGUSER:=postgres}"

DB_TYPE="${1:?Usage: $0 {control|demos|test|org_<slug>}}"

PSQL=(psql -h "$PGHOST" -p "$PGPORT" -U "$PGUSER" -v ON_ERROR_STOP=1)

case "$DB_TYPE" in
    control)
        DB_NAME="${CONTROL_DB_NAME:-scopenos_control}"
        echo "[grant] Granting scopenos_control_rw on ${DB_NAME}..."
        "${PSQL[@]}" -d "$DB_NAME" <<SQL
-- Connect privilege
GRANT CONNECT ON DATABASE "${DB_NAME}" TO scopenos_control_rw;

-- Schema usage
GRANT USAGE ON SCHEMA public TO scopenos_control_rw;

-- All existing tables
GRANT ALL ON ALL TABLES IN SCHEMA public TO scopenos_control_rw;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO scopenos_control_rw;

-- Future tables
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO scopenos_control_rw;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO scopenos_control_rw;

-- Deny access from PUBLIC (belt-and-suspenders)
REVOKE CONNECT ON DATABASE "${DB_NAME}" FROM PUBLIC;
SQL
        echo "[grant] Done: scopenos_control_rw has full access to ${DB_NAME}."
        ;;

    demos)
        DB_NAME="${DEMOS_DB_NAME:-scopenos_demos}"
        echo "[grant] Granting demos roles on ${DB_NAME}..."
        "${PSQL[@]}" -d "$DB_NAME" <<SQL
-- Writer role
GRANT CONNECT ON DATABASE "${DB_NAME}" TO scopenos_demos_writer;
GRANT USAGE ON SCHEMA public TO scopenos_demos_writer;
GRANT ALL ON ALL TABLES IN SCHEMA public TO scopenos_demos_writer;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO scopenos_demos_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO scopenos_demos_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO scopenos_demos_writer;

-- Reader role (SELECT only)
GRANT CONNECT ON DATABASE "${DB_NAME}" TO scopenos_demos_reader;
GRANT USAGE ON SCHEMA public TO scopenos_demos_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO scopenos_demos_reader;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA public TO scopenos_demos_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO scopenos_demos_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON SEQUENCES TO scopenos_demos_reader;

-- Deny writes to reader even if inherited grants exist
REVOKE INSERT, UPDATE, DELETE, TRUNCATE
    ON ALL TABLES IN SCHEMA public FROM scopenos_demos_reader;

-- Deny access from PUBLIC
REVOKE CONNECT ON DATABASE "${DB_NAME}" FROM PUBLIC;
SQL
        echo "[grant] Done: scopenos_demos_writer and scopenos_demos_reader set up on ${DB_NAME}."
        ;;

    test)
        DB_NAME="${TEST_DB_NAME:-phronosis_test}"
        echo "[grant] Granting scopenos_test_runner on ${DB_NAME}..."
        "${PSQL[@]}" -d "$DB_NAME" <<SQL
GRANT CONNECT ON DATABASE "${DB_NAME}" TO scopenos_test_runner;
GRANT USAGE ON SCHEMA public TO scopenos_test_runner;
GRANT ALL ON ALL TABLES IN SCHEMA public TO scopenos_test_runner;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO scopenos_test_runner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO scopenos_test_runner;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO scopenos_test_runner;

-- test_runner must NOT reach the control DB or org_* DBs
-- (those REVOKEs happen when each DB is granted — PUBLIC is denied by default above)
SQL
        echo "[grant] Done: scopenos_test_runner has full access to ${DB_NAME}."
        ;;

    org_*)
        DB_NAME="$DB_TYPE"
        ORG_ROLE="${DB_NAME}_rw"
        echo "[grant] Granting ${ORG_ROLE} on ${DB_NAME}..."
        # The org role is expected to exist already (created by provision_org.py).
        # This script just ensures the grants are correct.
        "${PSQL[@]}" -d "$DB_NAME" <<SQL
GRANT CONNECT ON DATABASE "${DB_NAME}" TO ${ORG_ROLE};
GRANT USAGE ON SCHEMA public TO ${ORG_ROLE};
GRANT ALL ON ALL TABLES IN SCHEMA public TO ${ORG_ROLE};
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ${ORG_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON TABLES TO ${ORG_ROLE};
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT ALL ON SEQUENCES TO ${ORG_ROLE};
REVOKE CONNECT ON DATABASE "${DB_NAME}" FROM PUBLIC;
SQL
        echo "[grant] Done: ${ORG_ROLE} has full access to ${DB_NAME}."
        ;;

    *)
        echo "Unknown DB type: ${DB_TYPE}" >&2
        echo "Usage: $0 {control|demos|test|org_<slug>}" >&2
        exit 1
        ;;
esac
