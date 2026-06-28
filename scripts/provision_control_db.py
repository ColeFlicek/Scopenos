#!/usr/bin/env python3
"""Create the scopenos_control_rw role and print the CONTROL_DB_URL to use.

Run once against the live scopenos DB as a superuser. Idempotent — safe to
re-run if the role already exists (will print the existing DSN from env or
prompt for the password).

Usage:
    SUPERUSER_DSN=postgresql://scopenos:<pw>@172.21.0.1/scopenos \
        python scripts/provision_control_db.py

Prints the generated CONTROL_DB_URL to stdout. Set it as a GitHub Secret.

Why a separate role:
    CONTROL_DB_URL is injected into the running server pod. Restricting it to
    read/write on the four control plane tables (organizations, users, api_keys,
    org_members) means a compromised server process cannot touch org databases,
    create databases, or escalate privileges. Contracts enforced against this
    role give a hard boundary: if the server tries to access anything outside
    the control plane, the DB rejects it.
"""
import asyncio
import os
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

ROLE = "scopenos_control_rw"
CONTROL_TABLES = ["organizations", "users", "api_keys", "org_members"]


async def main() -> None:
    import asyncpg

    superuser_dsn = os.environ.get("SUPERUSER_DSN")
    if not superuser_dsn:
        print(
            "ERROR: SUPERUSER_DSN must be set to a superuser DSN for the scopenos DB.\n"
            "Example: SUPERUSER_DSN=postgresql://scopenos:<pw>@172.21.0.1/scopenos",
            file=sys.stderr,
        )
        sys.exit(1)

    conn = await asyncpg.connect(superuser_dsn)

    try:
        existing = await conn.fetchrow(
            "SELECT rolname FROM pg_roles WHERE rolname = $1", ROLE
        )

        if existing:
            print(f"Role '{ROLE}' already exists.", file=sys.stderr)
            print(
                "Re-running grants to ensure all tables are covered...",
                file=sys.stderr,
            )
            password = None
        else:
            password = secrets.token_urlsafe(32)
            await conn.execute(
                f"CREATE ROLE {ROLE} LOGIN PASSWORD $1", password
            )
            print(f"Created role '{ROLE}'.", file=sys.stderr)

        # Extract host/port/dbname from the superuser DSN to build the output URL
        parsed = await conn.fetchrow(
            "SELECT current_database() AS db, inet_server_addr() AS host, "
            "inet_server_port() AS port"
        )
        db = parsed["db"]
        host = parsed["host"] or "172.21.0.1"
        port = parsed["port"] or 5432

        # Grant CONNECT on the control database
        await conn.execute(f"GRANT CONNECT ON DATABASE {db} TO {ROLE}")

        # Grant table-level DML — no DDL, no superuser, no other databases
        tables = ", ".join(CONTROL_TABLES)
        await conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {tables} TO {ROLE}"
        )

        # Grant sequence usage (needed for any future SERIAL columns)
        await conn.execute(
            f"GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO {ROLE}"
        )

        # Revoke default public schema create (defence in depth)
        await conn.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")

        print(f"Granted DML on: {tables}", file=sys.stderr)
        print(f"Revoked CREATE on public schema from PUBLIC.", file=sys.stderr)

    finally:
        await conn.close()

    if password:
        dsn = f"postgresql://{ROLE}:{password}@{host}:{port}/{db}"
        print(f"\nCONTROL_DB_URL (set this as a GitHub Secret):\n{dsn}\n")
        print(
            "Store this password securely — it is not saved anywhere.",
            file=sys.stderr,
        )
    else:
        print(
            "\nRole already existed — password not regenerated.\n"
            "If you need to reset the password, run:\n"
            f"  ALTER ROLE {ROLE} PASSWORD 'newpassword';",
            file=sys.stderr,
        )


if __name__ == "__main__":
    asyncio.run(main())
