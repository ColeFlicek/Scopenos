#!/usr/bin/env python3
"""Bootstrap: create a user + API key in the control DB.

Usage:
    CONTROL_DB_URL=postgresql://... python3 scripts/bootstrap_api_key.py [--admin]

Flags:
    --admin   Create an admin key (is_admin=TRUE). Admin keys can access /admin/*
              but cannot use MCP project tools. Use for the dashboard only.
              Without --admin, creates a key with org_id=NULL — use psql to set
              org_id after creation if an org-scoped key is needed.

Options (env vars):
    CONTROL_DB_URL or DATABASE_URL  — connection string (required)
    BOOTSTRAP_EMAIL                 — user email (default: cole.flicek@gmail.com)
    BOOTSTRAP_KEY_NAME              — label for the key (default: derived from type)
"""
import asyncio
import hashlib
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone


async def main(is_admin: bool) -> None:
    import asyncpg

    dsn = (
        os.environ.get("CONTROL_DB_URL")
        or os.environ.get("DATABASE_URL", "postgresql://scopenos:scopenos@172.21.0.1/scopenos")
    )
    conn = await asyncpg.connect(dsn)

    now = datetime.now(timezone.utc).isoformat()
    email = os.environ.get("BOOTSTRAP_EMAIL", "cole.flicek@gmail.com")
    key_name = os.environ.get("BOOTSTRAP_KEY_NAME", "cole-admin" if is_admin else "cole-primary")

    existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if existing:
        user_id = str(existing["id"])
        print(f"User exists: {user_id}", file=sys.stderr)
    else:
        user_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO users (id, email, created_at) VALUES ($1, $2, $3)",
            user_id, email, now,
        )
        print(f"Created user: {user_id}", file=sys.stderr)

    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    await conn.execute(
        """INSERT INTO api_keys (id, user_id, key_hash, name, created_at, is_admin)
           VALUES ($1, $2, $3, $4, $5, $6)""",
        str(uuid.uuid4()), user_id, key_hash, key_name, now, is_admin,
    )
    await conn.close()

    scope = "ADMIN" if is_admin else "ORG-SCOPED"
    print(f"\n[{scope}] NEW API KEY (save this — not stored in DB):\n{raw_key}\n", file=sys.stderr)
    if is_admin:
        print("Store in git config:  git config scopenos.apikey <key>", file=sys.stderr)
        print("Access dashboard at:  http://100.71.88.106:3004/admin/", file=sys.stderr)
    else:
        print("Update .mcp.json with this key, then restart the indexer session.", file=sys.stderr)

    # Print raw key to stdout for scripting
    print(raw_key)


if __name__ == "__main__":
    admin = "--admin" in sys.argv
    asyncio.run(main(is_admin=admin))
