#!/usr/bin/env python3
"""
Create a Scopenos user and issue an API key.

Usage:
    python scripts/create_user.py <email> [--name KEY_NAME] [--plan free|paid]
    python scripts/create_user.py <email> --project <project_id> --role owner

The raw API key is printed once and never stored — save it immediately.
Reads DATABASE_URL env var (default: postgresql://scopenos:scopenos@localhost/scopenos).

Examples:
    python scripts/create_user.py cole@example.com
    python scripts/create_user.py cole@example.com --name "dev laptop" --plan paid
    python scripts/create_user.py cole@example.com --project scopenos --role owner
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

# Allow running from the project root without installing the package.
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.call_graph.storage import CallGraphDB


async def main(email: str, key_name: str, plan: str, project_id: str, role: str) -> None:
    dsn = os.getenv("DATABASE_URL", "postgresql://scopenos:scopenos@localhost/scopenos")
    db = await CallGraphDB.create(dsn)

    try:
        user = await db.create_user(email, plan=plan)
        print(f"Created user: {user['email']} (id: {user['id']}, plan: {user['plan']})")
    except Exception:
        # User already exists — look them up and issue a new key.
        async with db._db.execute(
            "SELECT id, email, plan FROM users WHERE email = $1", (email,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            print(f"Error: could not create or find user '{email}'", file=sys.stderr)
            sys.exit(1)
        user = dict(row)
        print(f"User already exists: {user['email']} (id: {user['id']}, plan: {user['plan']})")

    if project_id:
        await db._db.execute(
            """INSERT INTO project_access (user_id, project_id, role)
               VALUES ($1, $2, $3)
               ON CONFLICT (user_id, project_id) DO UPDATE SET role = excluded.role""",
            (user["id"], project_id, role),
        )
        await db._db.commit()
        print(f"Granted {role} access to project '{project_id}'")

    raw_key = await db.create_api_key(user["id"], name=key_name)

    print()
    print("API key (shown once — copy it now):")
    print(f"  {raw_key}")
    print()
    print("Add to Claude Code MCP config (~/.claude.json):")
    print(f'  "headers": {{"X-API-Key": "{raw_key}"}}')

    await db.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Create a Scopenos user and issue an API key.")
    parser.add_argument("email", help="User's email address")
    parser.add_argument("--name", default="", help="Label for this key (e.g. 'dev laptop')")
    parser.add_argument("--plan", default="free", choices=["free", "paid"], help="User plan")
    parser.add_argument("--project", default="", metavar="PROJECT_ID", help="Grant access to this project")
    parser.add_argument("--role", default="owner", choices=["owner", "viewer"], help="Role for --project (default: owner)")
    args = parser.parse_args()
    asyncio.run(main(args.email, args.name, args.plan, args.project, args.role))


if __name__ == "__main__":
    cli()
