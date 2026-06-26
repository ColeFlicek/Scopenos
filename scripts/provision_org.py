#!/usr/bin/env python3
"""CLI: provision or teardown an org database.

Usage:
  python scripts/provision_org.py provision <slug> \\
      --provisioner-dsn DSN --control-dsn DSN [--schema schema_org.sql]

  python scripts/provision_org.py teardown <slug> \\
      --provisioner-dsn DSN --control-dsn DSN

Environment variable fallbacks:
  PROVISIONER_DSN   used when --provisioner-dsn is not given
  CONTROL_DSN       used when --control-dsn is not given

Exit codes:
  0 — success
  1 — usage error or provisioning failure
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

# Make src importable when running from project root or scripts/
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.provisioning import provision_org, teardown_org


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision_org.py",
        description="Provision or teardown an org database in Scopenos.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── provision ──────────────────────────────────────────────────────────────
    prov = sub.add_parser("provision", help="Create a new org database and role.")
    prov.add_argument("slug", help="Org slug (e.g. 'acme'). Becomes org_acme DB.")
    prov.add_argument(
        "--provisioner-dsn",
        default=os.getenv("PROVISIONER_DSN", ""),
        help="DSN for a role with CREATEDB privilege (env: PROVISIONER_DSN).",
    )
    prov.add_argument(
        "--control-dsn",
        default=os.getenv("CONTROL_DSN", ""),
        help="DSN for the scopenos_control DB (env: CONTROL_DSN).",
    )
    prov.add_argument(
        "--schema",
        default="schema_org.sql",
        help="Path to schema_org.sql (default: schema_org.sql relative to project root).",
    )

    # ── teardown ───────────────────────────────────────────────────────────────
    down = sub.add_parser("teardown", help="Drop an org's database and role.")
    down.add_argument("slug", help="Org slug to tear down.")
    down.add_argument(
        "--provisioner-dsn",
        default=os.getenv("PROVISIONER_DSN", ""),
        help="DSN for a role with CREATEDB/DROPDB privilege (env: PROVISIONER_DSN).",
    )
    down.add_argument(
        "--control-dsn",
        default=os.getenv("CONTROL_DSN", ""),
        help="DSN for the scopenos_control DB (env: CONTROL_DSN).",
    )

    return parser


async def _run(args: argparse.Namespace) -> int:
    if not args.provisioner_dsn:
        print("Error: --provisioner-dsn is required (or set PROVISIONER_DSN).", file=sys.stderr)
        return 1
    if not args.control_dsn:
        print("Error: --control-dsn is required (or set CONTROL_DSN).", file=sys.stderr)
        return 1

    if args.command == "provision":
        print(f"[provision] Provisioning org '{args.slug}'...")
        try:
            result = await provision_org(
                slug=args.slug,
                provisioner_dsn=args.provisioner_dsn,
                control_dsn=args.control_dsn,
                schema_sql_path=args.schema,
            )
        except Exception as exc:
            print(f"[provision] Failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    elif args.command == "teardown":
        print(f"[teardown] Tearing down org '{args.slug}'...")
        try:
            result = await teardown_org(
                slug=args.slug,
                provisioner_dsn=args.provisioner_dsn,
                control_dsn=args.control_dsn,
            )
        except Exception as exc:
            print(f"[teardown] Failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2))
        return 0

    return 1


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    sys.exit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
