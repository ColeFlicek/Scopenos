#!/usr/bin/env python3
"""Revoke a leaked API key and issue a replacement.

Usage:
    DATABASE_URL=postgresql://... python3 scripts/rotate_api_key.py <leaked_key>

Prints the new raw key to stdout. Store it immediately — it is never saved to the DB.
"""
import asyncio
import hashlib
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone


async def main(leaked_key: str) -> None:
    import asyncpg

    dsn = os.environ.get("CONTROL_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: CONTROL_DB_URL or DATABASE_URL must be set", file=sys.stderr)
        sys.exit(1)

    conn = await asyncpg.connect(dsn)

    # 1. Revoke the leaked key
    leaked_hash = hashlib.sha256(leaked_key.encode()).hexdigest()
    now = datetime.now(timezone.utc).isoformat()

    result = await conn.execute(
        "UPDATE api_keys SET revoked_at = $1 WHERE key_hash = $2 AND revoked_at IS NULL",
        now, leaked_hash,
    )
    rows_affected = int(result.split()[-1])
    if rows_affected == 0:
        print("WARNING: leaked key not found in DB (may already be revoked or never stored)", file=sys.stderr)
    else:
        print(f"✓ Revoked leaked key (hash: {leaked_hash[:16]}...)", file=sys.stderr)

    # 2. Find the user this key belonged to (for issuing replacement)
    row = await conn.fetchrow(
        "SELECT user_id FROM api_keys WHERE key_hash = $1", leaked_hash
    )
    if not row:
        # Key wasn't found — list users so caller can pick one
        users = await conn.fetch("SELECT id, email FROM users ORDER BY created_at")
        if not users:
            print("ERROR: no users found in DB. Create a user first.", file=sys.stderr)
            await conn.close()
            sys.exit(1)
        print("\nAvailable users:", file=sys.stderr)
        for u in users:
            print(f"  {u['id']}  {u['email']}", file=sys.stderr)
        print("\nRe-run with USER_ID=<id> to issue key for a specific user.", file=sys.stderr)
        user_id = os.environ.get("USER_ID", str(users[0]["id"]))
        print(f"Defaulting to first user: {user_id}", file=sys.stderr)
    else:
        user_id = str(row["user_id"])

    # 3. Issue replacement key
    raw_key = secrets.token_urlsafe(32)
    new_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    await conn.execute(
        "INSERT INTO api_keys (id, user_id, key_hash, name, created_at) VALUES ($1, $2, $3, $4, $5)",
        str(uuid.uuid4()), user_id, new_hash, "replacement-after-github-leak", now,
    )

    await conn.close()

    print(f"✓ New API key issued for user {user_id}", file=sys.stderr)
    print(f"\nNEW KEY (save this now — not stored in DB):\n{raw_key}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <leaked_key>", file=sys.stderr)
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
