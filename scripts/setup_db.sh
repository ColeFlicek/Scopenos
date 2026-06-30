#!/bin/bash
# One-time database setup — run as postgres superuser.
# Creates the scopenos user, database, and enables pgvector.
#
# Usage (local dev):
#   sudo -u postgres bash scripts/setup_db.sh
#
# Usage (CI):
#   PGPASSWORD=postgres psql -U postgres -h localhost -f scripts/setup_db.sh

set -e

DB=${DB:-scopenos}
TEST_DB=${TEST_DB:-scopenos_test}
DB_USER=${DB_USER:-scopenos}
DB_PASS=${DB_PASS:-scopenos}

psql -v ON_ERROR_STOP=1 <<-EOSQL
    DO \$\$ BEGIN
        IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
            CREATE USER ${DB_USER} WITH PASSWORD '${DB_PASS}';
        END IF;
    END \$\$;

    SELECT 'CREATE DATABASE ${DB} OWNER ${DB_USER}'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${DB}')\gexec

    SELECT 'CREATE DATABASE ${TEST_DB} OWNER ${DB_USER}'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '${TEST_DB}')\gexec
EOSQL

# Enable pgvector in both databases
for db in "${DB}" "${TEST_DB}"; do
    psql -d "${db}" -c "CREATE EXTENSION IF NOT EXISTS vector;" 2>/dev/null || true
    psql -d "${db}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER};"
done

# Grant test runner full access on the test DB so CI can create project schemas
# and read/write all tables without superuser privileges.
if psql -tc "SELECT 1 FROM pg_roles WHERE rolname = 'scopenos_test_runner'" | grep -q 1; then
    psql -d "${TEST_DB}" -c "
        GRANT CONNECT ON DATABASE ${TEST_DB} TO scopenos_test_runner;
        GRANT CREATE ON DATABASE ${TEST_DB} TO scopenos_test_runner;
        GRANT ALL ON SCHEMA public TO scopenos_test_runner;
        GRANT CREATE ON SCHEMA public TO scopenos_test_runner;
        GRANT ALL ON ALL TABLES IN SCHEMA public TO scopenos_test_runner;
        GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO scopenos_test_runner;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO scopenos_test_runner;
        ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO scopenos_test_runner;
    "
    # Transfer table ownership so test_runner can run ALTER TABLE during schema init
    psql -d "${TEST_DB}" -c "REASSIGN OWNED BY ${DB_USER} TO scopenos_test_runner;" 2>/dev/null || true
    echo "Granted scopenos_test_runner access on ${TEST_DB}."
fi

echo "Database setup complete. Apply schema:"
echo "  PGPASSWORD=${DB_PASS} psql -U ${DB_USER} -d ${DB} -f schema.sql"
echo "  PGPASSWORD=${DB_PASS} psql -U ${DB_USER} -d ${TEST_DB} -f schema.sql"
