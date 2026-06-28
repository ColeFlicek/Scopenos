#!/usr/bin/env python3
"""One-time bootstrap: create Cole's user + API key in a freshly rebuilt DB."""
import asyncio
import hashlib
import os
import secrets
import uuid
from datetime import datetime, timezone


async def main() -> None:
    import asyncpg

    dsn = (
        os.environ.get("CONTROL_DB_URL")
        or os.environ.get("DATABASE_URL", "postgresql://scopenos:scopenos@172.21.0.1/scopenos")
    )
    conn = await asyncpg.connect(dsn)

    now = datetime.now(timezone.utc).isoformat()
    email = "cole.flicek@gmail.com"

    existing = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
    if existing:
        user_id = str(existing["id"])
        print(f"User exists: {user_id}")
    else:
        user_id = str(uuid.uuid4())
        await conn.execute(
            "INSERT INTO users (id, email, plan, created_at) VALUES ($1, $2, $3, $4)",
            user_id, email, "owner", now,
        )
        print(f"Created user: {user_id}")

    raw_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    await conn.execute(
        "INSERT INTO api_keys (id, user_id, key_hash, name, created_at) VALUES ($1, $2, $3, $4, $5)",
        str(uuid.uuid4()), user_id, key_hash, "cole-primary", now,
    )
    await conn.close()

    print(f"\nNEW API KEY (save this — not stored in DB):\n{raw_key}\n")
    print('Update .mcp.json with this key, then restart the indexer session.')


asyncio.run(main())
