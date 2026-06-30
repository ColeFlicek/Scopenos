#!/usr/bin/env python3
"""Apply incremental control-plane schema changes that require DDL privileges.

Run once with a superuser or DB-owner connection. The app role (scopenos_control_rw)
cannot run DDL — this script must use a privileged DSN.

Usage:
    SUPERUSER_DSN=postgresql://scopenos:<pw>@172.21.0.1/scopenos \
        python3 scripts/migrate_control_plane.py

    # Or via port-forward from a machine with kubectl:
    kubectl port-forward -n scopenos postgres-0 5432:5432 &
    SUPERUSER_DSN=postgresql://scopenos:<pw>@localhost/scopenos \
        python3 scripts/migrate_control_plane.py
"""
import asyncio
import os
import sys


MIGRATION_SQL = """
-- is_admin column (Phase: admin dashboard)
ALTER TABLE api_keys
    ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE;

-- Auth events log (Phase: admin dashboard)
CREATE TABLE IF NOT EXISTS auth_events (
    id          TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL,
    key_prefix  TEXT,
    outcome     TEXT NOT NULL,
    endpoint    TEXT,
    project_id  TEXT
);

CREATE INDEX IF NOT EXISTS idx_auth_events_created
    ON auth_events(created_at DESC);

-- Grant DML on new tables to the app role (idempotent once role exists)
DO $$
BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'scopenos_control_rw') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON api_keys, auth_events
            TO scopenos_control_rw;
    END IF;
END $$;
"""


async def main() -> None:
    import asyncpg

    dsn = os.environ.get("SUPERUSER_DSN")
    if not dsn:
        print(
            "ERROR: SUPERUSER_DSN must be set to a superuser/owner DSN for the scopenos DB.\n"
            "  SUPERUSER_DSN=postgresql://scopenos:<pw>@172.21.0.1/scopenos python3 scripts/migrate_control_plane.py",
            file=sys.stderr,
        )
        sys.exit(1)

    print("Connecting to control DB...", file=sys.stderr)
    conn = await asyncpg.connect(dsn)
    try:
        await conn.execute(MIGRATION_SQL)
        print("Migration applied successfully.", file=sys.stderr)
        print("  ✓ api_keys.is_admin column", file=sys.stderr)
        print("  ✓ auth_events table + index", file=sys.stderr)
        print("  ✓ scopenos_control_rw grants updated", file=sys.stderr)
    except Exception as e:
        print(f"Migration failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
