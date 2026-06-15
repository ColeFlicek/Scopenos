#!/bin/bash
# One-time database setup — run as postgres superuser.
# Creates the phronosis user, database, and enables pgvector.
#
# Usage (local dev):
#   sudo -u postgres bash scripts/setup_db.sh
#
# Usage (CI):
#   PGPASSWORD=postgres psql -U postgres -h localhost -f scripts/setup_db.sh

set -e

DB=${DB:-phronosis}
TEST_DB=${TEST_DB:-phronosis_test}
DB_USER=${DB_USER:-phronosis}
DB_PASS=${DB_PASS:-phronosis}

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

echo "Database setup complete. Apply schema:"
echo "  PGPASSWORD=${DB_PASS} psql -U ${DB_USER} -d ${DB} -f schema.sql"
echo "  PGPASSWORD=${DB_PASS} psql -U ${DB_USER} -d ${TEST_DB} -f schema.sql"
