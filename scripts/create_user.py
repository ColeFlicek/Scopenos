#!/usr/bin/env python3
"""
Create a Scopenos user and issue an API key.

Usage:
    python scripts/create_user.py <email> [--name KEY_NAME] [--plan free|paid]
    python scripts/create_user.py <email> --project <project_id> --role owner
    python scripts/create_user.py <email> --org-id <slug>   # assign to an org (multi-org mode)

The raw API key is printed once and never stored — save it immediately.
Reads CONTROL_DB_URL then DATABASE_URL (default: postgresql://scopenos:scopenos@localhost/scopenos).

In multi-org mode:
  - Users and API keys always live in the control DB.
  - Pass --org-id to associate the key with a specific org database.
  - The org must already be provisioned (scripts/provision_org.py provision <slug>).

Examples:
    python scripts/create_user.py cole@example.com
    python scripts/create_user.py cole@example.com --name "dev laptop" --plan paid
    python scripts/create_user.py cole@example.com --project scopenos --role owner
    python scripts/create_user.py cole@example.com --org-id personal
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.call_graph.storage import CallGraphDB


async def main(
    email: str,
    key_name: str,
    plan: str,
    project_id: str,
    role: str,
    org_id: str,
) -> None:
    dsn = (
        os.getenv("CONTROL_DB_URL")
        or os.getenv("DATABASE_URL", "postgresql://scopenos:scopenos@localhost/scopenos")
    )
    db = await CallGraphDB.create(dsn)

    try:
        user = await db.create_user(email, plan=plan)
        print(f"Created user: {user['email']} (id: {user['id']}, plan: {user['plan']})")
    except Exception:
        async with db._db.execute(
            "SELECT id, email, plan FROM users WHERE email = ?", (email,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            print(f"Error: could not create or find user '{email}'", file=sys.stderr)
            sys.exit(1)
        user = dict(row)
        print(f"User already exists: {user['email']} (id: {user['id']}, plan: {user['plan']})")

    if org_id:
        # Verify the org exists before associating the key
        async with db._db.execute(
            "SELECT id FROM organizations WHERE slug = ?", (org_id,)
        ) as cur:
            org_row = await cur.fetchone()
        if org_row is None:
            print(
                f"Error: org '{org_id}' not found in control DB. "
                f"Run: python scripts/provision_org.py provision {org_id}",
                file=sys.stderr,
            )
            await db.close()
            sys.exit(1)
        # Set org_id on the user row
        async with db._db.execute(
            "UPDATE users SET org_id = ? WHERE id = ?",
            (org_id, user["id"]),
        ):
            pass
        print(f"Associated user with org '{org_id}'")

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
    if org_id:
        print(f"  (routes to org '{org_id}' database)")

    await db.close()


def cli() -> None:
    parser = argparse.ArgumentParser(description="Create a Scopenos user and issue an API key.")
    parser.add_argument("email", help="User's email address")
    parser.add_argument("--name", default="", help="Label for this key (e.g. 'dev laptop')")
    parser.add_argument("--plan", default="free", choices=["free", "paid"], help="User plan")
    parser.add_argument("--project", default="", metavar="PROJECT_ID", help="Grant access to this project")
    parser.add_argument("--role", default="owner", choices=["owner", "viewer"], help="Role for --project (default: owner)")
    parser.add_argument(
        "--org-id",
        default="",
        metavar="SLUG",
        help="Org slug to associate this user with (multi-org mode). "
             "The org must already exist (provision_org.py provision <slug>).",
    )
    args = parser.parse_args()
    asyncio.run(main(args.email, args.name, args.plan, args.project, args.role, args.org_id))


if __name__ == "__main__":
    cli()
