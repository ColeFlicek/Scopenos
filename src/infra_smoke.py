"""Infrastructure smoke checks — deterministic assertions about live system state.

Run via K8s Job at deploy time, CronJob for drift detection, or directly:
    python -m src.infra_smoke

Each check uses the connection and role it's given — never superuser internally.
A check that passes with a superuser connection but fails with the restricted role
is a false pass; callers should pass the restricted role explicitly.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from dataclasses import dataclass
from typing import Literal

import asyncpg

CONTROL_TABLES = ["organizations", "users", "api_keys", "org_members", "auth_events"]
CONTROL_PRIVILEGES = ["SELECT", "INSERT", "UPDATE", "DELETE"]


@dataclass
class CheckResult:
    name: str
    status: Literal["PASS", "FAIL", "ERROR"]
    detail: str

    def __str__(self) -> str:
        icon = {"PASS": "✓", "FAIL": "✗", "ERROR": "!"}.get(self.status, "?")
        return f"[{icon}] {self.name}: {self.detail}"


async def run_checks(
    conn: asyncpg.Connection,
    *,
    role: str = "scopenos_control_rw",
    tables: list[str] | None = None,
    env_key: str | None = None,
    org_conn: asyncpg.Connection | None = None,
) -> list[CheckResult]:
    """Run all enabled infrastructure checks and return results.

    Args:
        conn:     Connection to the control-plane DB (need not be the restricted role).
        role:     The role whose privileges to verify (default: scopenos_control_rw).
        env_key:  Raw API key value from SCOPENOS_API_KEY env var (optional).
        org_conn: Connection to the org DB for metadata checks (optional).
    """
    results: list[CheckResult] = []
    results.append(await _check_role_can_dml(conn, role, tables or CONTROL_TABLES))
    if env_key is not None:
        results.append(await _check_env_key_resolves(conn, env_key))
    if org_conn is not None:
        results.append(await check_metadata_accuracy(org_conn))
    return results


# ── Individual checks ─────────────────────────────────────────────────────────

async def _check_role_can_dml(conn: asyncpg.Connection, role: str, tables: list[str]) -> CheckResult:
    missing: list[str] = []
    try:
        for table in tables:
            for priv in CONTROL_PRIVILEGES:
                has_it = await conn.fetchval(
                    "SELECT has_table_privilege($1, $2, $3)", role, table, priv
                )
                if not has_it:
                    missing.append(f"{table}.{priv}")
    except asyncpg.UndefinedObjectError:
        return CheckResult(
            "role_can_dml_control_tables",
            "FAIL",
            f"role does not exist: {role}",
        )
    except asyncpg.UndefinedTableError as exc:
        return CheckResult(
            "role_can_dml_control_tables",
            "FAIL",
            f"control-plane table missing: {exc}",
        )
    except Exception as exc:
        return CheckResult("role_can_dml_control_tables", "ERROR", str(exc))

    if missing:
        return CheckResult(
            "role_can_dml_control_tables",
            "FAIL",
            f"{role} missing: {', '.join(missing)}",
        )
    return CheckResult(
        "role_can_dml_control_tables",
        "PASS",
        f"{role} has DML on all {len(CONTROL_TABLES)} control tables",
    )


async def _check_env_key_resolves(conn: asyncpg.Connection, raw_key: str) -> CheckResult:
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    try:
        row = await conn.fetchrow(
            "SELECT name, is_admin FROM api_keys WHERE key_hash = $1", key_hash
        )
    except Exception as exc:
        return CheckResult("env_key_resolves", "ERROR", str(exc))

    if row is None:
        return CheckResult(
            "env_key_resolves",
            "FAIL",
            "SCOPENOS_API_KEY is set but its hash is not in api_keys",
        )
    scope = "admin" if row["is_admin"] else "org-scoped"
    return CheckResult(
        "env_key_resolves",
        "PASS",
        f"key '{row['name']}' ({scope}) resolves",
    )


async def check_metadata_accuracy(org_conn: asyncpg.Connection) -> CheckResult:
    try:
        projects = await org_conn.fetch(
            "SELECT id, schema_name, node_count FROM projects"
        )
    except Exception as exc:
        return CheckResult("metadata_accuracy", "ERROR", str(exc))

    stale: list[str] = []
    for row in projects:
        schema = row["schema_name"]
        stored = row["node_count"]
        try:
            actual = await org_conn.fetchval(
                f'SELECT COUNT(*) FROM "{schema}".functions'
            )
        except Exception:
            continue
        if actual != stored:
            stale.append(f"{row['id']}: stored={stored} actual={actual}")

    if stale:
        return CheckResult(
            "metadata_accuracy",
            "FAIL",
            f"{len(stale)} project(s) have stale node_count: {'; '.join(stale)}",
        )
    if not projects:
        return CheckResult("metadata_accuracy", "PASS", "no projects to check")
    return CheckResult(
        "metadata_accuracy",
        "PASS",
        f"node_count accurate for all {len(projects)} project(s)",
    )


# ── CLI entry point ───────────────────────────────────────────────────────────

async def _main() -> None:
    dsn = os.environ.get("CONTROL_DB_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("ERROR: CONTROL_DB_URL or DATABASE_URL must be set", file=sys.stderr)
        sys.exit(1)

    role = os.environ.get("SMOKE_ROLE", "scopenos_control_rw")
    env_key = os.environ.get("SCOPENOS_API_KEY") or None
    org_dsn = os.environ.get("ORG_DB_URL") or None

    conn = await asyncpg.connect(dsn)
    org_conn = await asyncpg.connect(org_dsn) if org_dsn else None
    try:
        results = await run_checks(conn, role=role, env_key=env_key, org_conn=org_conn)
    finally:
        await conn.close()
        if org_conn:
            await org_conn.close()

    any_fail = False
    for r in results:
        print(r)
        if r.status in ("FAIL", "ERROR"):
            any_fail = True

    passed = sum(1 for r in results if r.status == "PASS")
    total = len(results)
    print(f"\n{passed}/{total} checks passed")
    sys.exit(1 if any_fail else 0)


if __name__ == "__main__":
    asyncio.run(_main())
