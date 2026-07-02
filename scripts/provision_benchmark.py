#!/usr/bin/env python3
"""One-shot: provision org_benchmark + create a benchmark API key.

Run once from TheHive (needs CREATEDB privilege via PROVISIONER_DSN):

    PROVISIONER_DSN="postgresql://scopenos_provisioner:<pw>@localhost/postgres" \
    CONTROL_DSN="postgresql://scopenos_control_rw:<pw>@localhost/scopenos" \
    python3 scripts/provision_benchmark.py

The script prints the generated API key to stdout. Add it to your K8s
secret as BENCH_API_KEY so the benchmark CronJob can index into org_benchmark.

Safe to re-run: provision_org is idempotent (CREATE IF NOT EXISTS), and
the API key creation is gated on whether a benchmark-indexer key already exists.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.provisioning import provision_org


async def main() -> None:
    provisioner_dsn = os.environ.get("PROVISIONER_DSN", "")
    control_dsn = os.environ.get("CONTROL_DSN", "")

    if not provisioner_dsn:
        print("Error: PROVISIONER_DSN is required.", file=sys.stderr)
        print("  export PROVISIONER_DSN='postgresql://scopenos_provisioner:<pw>@localhost/postgres'", file=sys.stderr)
        sys.exit(1)
    if not control_dsn:
        print("Error: CONTROL_DSN is required.", file=sys.stderr)
        print("  export CONTROL_DSN='postgresql://scopenos_control_rw:<pw>@localhost/scopenos'", file=sys.stderr)
        sys.exit(1)

    import asyncpg

    # ── 1. Provision org_benchmark ────────────────────────────────────────────
    print("[1/3] Provisioning org_benchmark...", file=sys.stderr)
    try:
        result = await provision_org(
            slug="benchmark",
            provisioner_dsn=provisioner_dsn,
            control_dsn=control_dsn,
            schema_sql_path="schema_org.sql",
        )
        print(f"      db: {result['db_name']}", file=sys.stderr)
        print(f"      role: {result['role_name']}", file=sys.stderr)
    except Exception as exc:
        if "already exists" in str(exc):
            print("      org_benchmark already exists — skipping", file=sys.stderr)
        else:
            print(f"Error provisioning org: {exc}", file=sys.stderr)
            sys.exit(1)

    # ── 2. Create benchmark-indexer user + API key in control DB ─────────────
    print("[2/3] Creating benchmark-indexer API key in control DB...", file=sys.stderr)
    conn = await asyncpg.connect(control_dsn)
    try:
        await conn.execute("SET search_path TO scopenos, public")

        # Check for existing benchmark-indexer key
        existing_key = await conn.fetchrow(
            "SELECT id FROM api_keys WHERE name = 'benchmark-indexer'"
        )
        if existing_key:
            print("      benchmark-indexer key already exists — skipping key creation", file=sys.stderr)
            print("      To rotate: DELETE FROM api_keys WHERE name='benchmark-indexer'; re-run this script.", file=sys.stderr)
            await conn.close()
            print("[3/3] Done. No new key generated (existing key unchanged).", file=sys.stderr)
            return

        now = datetime.now(timezone.utc).isoformat()
        email = "benchmark@scopenos.internal"

        existing_user = await conn.fetchrow("SELECT id FROM users WHERE email = $1", email)
        if existing_user:
            user_id = str(existing_user["id"])
        else:
            user_id = str(uuid.uuid4())
            await conn.execute(
                "INSERT INTO users (id, email, created_at) VALUES ($1, $2, $3)",
                user_id, email, now,
            )

        raw_key = "scopenos-bench-" + secrets.token_urlsafe(24)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        await conn.execute(
            """INSERT INTO api_keys (id, user_id, key_hash, name, org_id, created_at, is_admin)
               VALUES ($1, $2, $3, $4, $5, $6, FALSE)""",
            str(uuid.uuid4()), user_id, key_hash, "benchmark-indexer", "benchmark", now,
        )
        print(f"      user: {email} ({user_id})", file=sys.stderr)
        print(f"      key name: benchmark-indexer", file=sys.stderr)
        print(f"      org_id: benchmark → routes to org_benchmark DB", file=sys.stderr)

    finally:
        await conn.close()

    # ── 3. Print the key ──────────────────────────────────────────────────────
    print("[3/3] Done.", file=sys.stderr)
    print(file=sys.stderr)
    print("BENCH_API_KEY (add to K8s secret and benchmark env):", file=sys.stderr)
    print(raw_key)  # stdout only — pipe to clipboard or secret manager


if __name__ == "__main__":
    asyncio.run(main())
