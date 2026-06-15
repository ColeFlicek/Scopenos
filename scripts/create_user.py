#!/usr/bin/env python3
"""
Create an Phronosis user and issue an API key.

Usage:
    python scripts/create_user.py <email> [--name KEY_NAME] [--plan free|paid]

The raw API key is printed once and never stored — save it immediately.
Reads SQLITE_PATH env var (default: /data/phronosis.db).

Examples:
    python scripts/create_user.py cole@example.com
    python scripts/create_user.py cole@example.com --name "dev laptop" --plan paid
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.call_graph.storage import CallGraphDB


async def main(email: str, key_name: str, plan: str) -> None:
    db_path = os.getenv("SQLITE_PATH", "/data/phronosis.db")
    db = await CallGraphDB.create(db_path)

    try:
        user = await db.create_user(email, plan=plan)
        print(f"Created user: {user['email']} (id: {user['id']}, plan: {user['plan']})")
    except Exception:
        # User already exists — look them up and issue a new key.
        async with db._db.execute(
            "SELECT id, email, plan FROM users WHERE email = ?", (email,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            print(f"Error: could not create or find user '{email}'", file=sys.stderr)
            sys.exit(1)
        user = dict(row)
        print(f"User already exists: {user['email']} (id: {user['id']}, plan: {user['plan']})")

    raw_key = await db.create_api_key(user["id"], name=key_name)

    print()
    print("API key (shown once — copy it now):")
    print(f"  {raw_key}")
    print()
    print("Add to Claude Code MCP config:")
    print(f'  "headers": {{"X-API-Key": "{raw_key}"}}')

    await db.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Create an Phronosis user and issue an API key.")
    parser.add_argument("email", help="User's email address")
    parser.add_argument("--name", default="", help="Label for this key (e.g. 'dev laptop')")
    parser.add_argument("--plan", default="free", choices=["free", "paid"], help="User plan")
    args = parser.parse_args()
    asyncio.run(main(args.email, args.name, args.plan))


if __name__ == "__main__":
    cli()
