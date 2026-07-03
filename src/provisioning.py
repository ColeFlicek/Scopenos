"""Org provisioning: create isolated database + role per organization.

Each org gets:
  - A dedicated Postgres database: org_{slug}
  - A login role with a generated password: org_{slug}_rw
  - The schema_org.sql schema applied to the new database
  - An entry in scopenos.organizations (if the control DB is reachable)

This module is called by the provisioning CLI (scripts/provision_org.py) and
can also be invoked programmatically during the signup flow.
"""
from __future__ import annotations

import asyncio
import os
import re
import secrets
import string
from pathlib import Path
from typing import Any

import asyncpg


# ── Password generation ───────────────────────────────────────────────────────

def generate_password(length: int = 32) -> str:
    """Generate a cryptographically secure random password.

    Uses only alphanumeric characters so the result is safe in connection
    strings without URL encoding.
    """
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


# ── SQL helpers ───────────────────────────────────────────────────────────────

def _validate_slug(slug: str) -> None:
    """Raise ValueError if slug is not a safe identifier component."""
    if not re.fullmatch(r"[a-z0-9][a-z0-9_]{0,62}", slug):
        raise ValueError(
            f"Invalid org slug {slug!r}. Must match [a-z0-9][a-z0-9_]{{0,62}}."
        )


def _db_name(slug: str) -> str:
    return f"org_{slug}"


def _role_name(slug: str) -> str:
    return f"org_{slug}_rw"


async def _apply_schema(conn: asyncpg.Connection, schema_sql_path: str) -> None:
    """Read and execute schema_sql_path against an already-connected database.

    Splits on statement boundaries carefully, preserving $$ function bodies.
    Skips blank / comment-only statements.
    """
    path = Path(schema_sql_path)
    if not path.is_absolute():
        # Resolve relative to the project root (two levels up from this file)
        path = Path(__file__).parent.parent / schema_sql_path

    sql_text = path.read_text()

    # Split into statements by splitting on semicolons that are NOT inside
    # dollar-quoted strings.  We track $$ nesting with a simple state machine.
    statements = _split_sql(sql_text)
    for stmt in statements:
        stmt = stmt.strip()
        # Strip leading comment lines so a statement like "-- comment\nCREATE TABLE..."
        # is not mistakenly treated as comment-only and skipped.
        sql_only = "\n".join(
            line for line in stmt.splitlines() if not line.strip().startswith("--")
        ).strip()
        if sql_only:
            await conn.execute(stmt)


def _split_sql(sql: str) -> list[str]:
    """Split SQL text into individual statements.

    Handles dollar-quoted string literals ($$...$$, $tag$...$tag$) and
    single-line comments (-- ... newline) which may contain semicolons that
    must NOT be treated as statement terminators.
    """
    statements: list[str] = []
    current: list[str] = []
    i = 0
    in_dollar_quote = False
    dollar_tag = ""

    while i < len(sql):
        # Single-line comment: consume through end of line without splitting.
        if not in_dollar_quote and sql[i] == "-" and sql[i:i+2] == "--":
            j = i
            while j < len(sql) and sql[j] != "\n":
                j += 1
            current.append(sql[i:j])
            i = j
            continue

        # Detect start/end of dollar-quoted string
        if not in_dollar_quote and sql[i] == "$":
            # Scan for closing $ of the tag
            j = i + 1
            while j < len(sql) and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            if j < len(sql) and sql[j] == "$":
                dollar_tag = sql[i : j + 1]
                in_dollar_quote = True
                current.append(sql[i : j + 1])
                i = j + 1
                continue
        elif in_dollar_quote and sql[i] == "$":
            # Check if this is the closing tag
            end = i + len(dollar_tag)
            if sql[i:end] == dollar_tag:
                in_dollar_quote = False
                current.append(dollar_tag)
                i = end
                continue

        if not in_dollar_quote and sql[i] == ";":
            current.append(";")
            statements.append("".join(current))
            current = []
        else:
            current.append(sql[i])
        i += 1

    # Flush any trailing content
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)

    return statements


# ── Control DB helpers ────────────────────────────────────────────────────────

async def _record_org(control_dsn: str, slug: str, db_name: str, role_name: str, conn_str: str) -> None:
    """Insert org record into scopenos.organizations if the table exists."""
    try:
        conn = await asyncpg.connect(control_dsn)
    except Exception:
        # Control DB may not be configured in all environments — skip silently
        return
    try:
        # Set search_path to scopenos schema where control plane tables live
        await conn.execute("SET search_path TO scopenos, public")
        # Check that the organizations table exists before inserting
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='organizations' AND table_schema IN ('scopenos','public')"
        )
        if not exists:
            return
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        await conn.execute(
            """
            INSERT INTO organizations (id, slug, db_url, plan, created_at)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (slug) DO UPDATE SET db_url = EXCLUDED.db_url, updated_at = EXCLUDED.updated_at
            """,
            slug,  # use slug as id for simplicity
            slug,
            conn_str,
            "free",
            now,
        )
    except Exception:
        # Non-fatal — provisioning succeeds even if control record fails
        pass
    finally:
        await conn.close()


async def _remove_org_record(control_dsn: str, slug: str) -> None:
    """Remove org record from scopenos.organizations if it exists."""
    try:
        conn = await asyncpg.connect(control_dsn)
    except Exception:
        return
    try:
        await conn.execute("SET search_path TO scopenos, public")
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_name='organizations' AND table_schema IN ('scopenos','public')"
        )
        if not exists:
            return
        await conn.execute("DELETE FROM organizations WHERE slug = $1", slug)
    except Exception:
        pass
    finally:
        await conn.close()


# ── Public API ────────────────────────────────────────────────────────────────

async def provision_org(
    slug: str,
    provisioner_dsn: str,
    control_dsn: str,
    schema_sql_path: str = "schema_org.sql",
) -> dict[str, Any]:
    """Provision a new organization.

    Steps:
      1. Validate the slug.
      2. Connect as scopenos_provisioner (needs CREATEDB privilege).
      3. CREATE DATABASE org_{slug} (outside a transaction).
      4. Connect to the new database as the provisioner.
      5. CREATE EXTENSION vector (requires superuser or pg_extension_owner).
      6. Apply schema_org.sql.
      7. CREATE ROLE org_{slug}_rw LOGIN PASSWORD '{generated}'.
      8. GRANT CONNECT ON DATABASE org_{slug} TO org_{slug}_rw.
      9. GRANT ALL ON ALL TABLES / SEQUENCES IN SCHEMA public TO org_{slug}_rw.
     10. REVOKE CONNECT ON DATABASE org_{slug} FROM PUBLIC.
     11. Record in scopenos.organizations (best-effort).

    Args:
        slug: Short org identifier, e.g. "acme".  Becomes part of DB/role names.
        provisioner_dsn: DSN for a role with CREATEDB privilege (e.g. scopenos_provisioner).
                         Must connect to the maintenance database (typically "postgres").
        control_dsn: DSN for the scopenos control database.  Used to register the org.
        schema_sql_path: Path to schema_org.sql, relative to project root or absolute.

    Returns:
        dict with keys: db_name, role_name, connection_string
    """
    _validate_slug(slug)
    db_name = _db_name(slug)
    role_name = _role_name(slug)
    password = generate_password()

    # Step 1: Create the database from template_vector, which has pgvector
    # pre-installed.  template_vector is marked datistemplate=true so any role
    # with CREATEDB can copy it — no superuser needed here.
    prov_conn = await asyncpg.connect(provisioner_dsn)
    try:
        await prov_conn.execute(f'CREATE DATABASE "{db_name}" TEMPLATE template_vector')
    finally:
        await prov_conn.close()

    # Step 2: Connect to the new database and apply the org schema.
    # Derive the org DB DSN from the provisioner DSN by replacing the dbname.
    parsed = _replace_dbname(provisioner_dsn, db_name)
    org_conn = await asyncpg.connect(parsed)
    try:
        # Apply org schema (idempotent: CREATE IF NOT EXISTS + ALTER IF NOT EXISTS)
        await _apply_schema(org_conn, schema_sql_path)

        # Create org role with login
        await org_conn.execute(
            f"CREATE ROLE \"{role_name}\" LOGIN PASSWORD '{password}'"
        )

        # Grant connect + create on the new DB.
        # CREATE is needed so org_scopenos_rw can call create_project_schema(),
        # which runs CREATE SCHEMA IF NOT EXISTS for each indexed project.
        await org_conn.execute(
            f'GRANT CONNECT, CREATE ON DATABASE "{db_name}" TO "{role_name}"'
        )

        # Grant usage on schema
        await org_conn.execute(
            f'GRANT USAGE ON SCHEMA public TO "{role_name}"'
        )

        # Grant all on existing tables/sequences
        await org_conn.execute(
            f'GRANT ALL ON ALL TABLES IN SCHEMA public TO "{role_name}"'
        )
        await org_conn.execute(
            f'GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO "{role_name}"'
        )

        # Future tables
        await org_conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT ALL ON TABLES TO "{role_name}"'
        )
        await org_conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA public '
            f'GRANT ALL ON SEQUENCES TO "{role_name}"'
        )

        # Deny PUBLIC connect
        await org_conn.execute(
            f'REVOKE CONNECT ON DATABASE "{db_name}" FROM PUBLIC'
        )
    finally:
        await org_conn.close()

    # Step 3: Build a connection string for the new org using its dedicated role.
    conn_string = _replace_user_and_dbname(provisioner_dsn, role_name, password, db_name)

    # Step 4: Record in control plane (best-effort, non-fatal).
    await _record_org(control_dsn, slug, db_name, role_name, conn_string)

    return {
        "db_name": db_name,
        "role_name": role_name,
        "connection_string": conn_string,
    }


async def teardown_org(
    slug: str,
    provisioner_dsn: str,
    control_dsn: str,
) -> dict[str, Any]:
    """Remove an org's database and role.

    Steps:
      1. DROP DATABASE IF EXISTS org_{slug}.
      2. DROP ROLE IF EXISTS org_{slug}_rw.
      3. Remove from scopenos.organizations (best-effort).

    Args:
        slug: Org identifier.
        provisioner_dsn: DSN for a role with CREATEDB/DROPDB privilege.
        control_dsn: DSN for the control database.

    Returns:
        dict with keys: dropped_db, dropped_role
    """
    _validate_slug(slug)
    db_name = _db_name(slug)
    role_name = _role_name(slug)

    prov_conn = await asyncpg.connect(provisioner_dsn)
    try:
        # Terminate existing connections so DROP DATABASE succeeds
        await prov_conn.execute(
            """
            SELECT pg_terminate_backend(pid)
            FROM pg_stat_activity
            WHERE datname = $1 AND pid <> pg_backend_pid()
            """,
            db_name,
        )
        await prov_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
        await prov_conn.execute(f'DROP ROLE IF EXISTS "{role_name}"')
    finally:
        await prov_conn.close()

    # Remove from control plane (best-effort)
    await _remove_org_record(control_dsn, slug)

    return {
        "dropped_db": db_name,
        "dropped_role": role_name,
    }


# ── DSN manipulation helpers ──────────────────────────────────────────────────

def _replace_dbname(dsn: str, new_dbname: str) -> str:
    """Return a new DSN with the database component replaced by new_dbname."""
    import urllib.parse as up
    parsed = up.urlparse(dsn)
    # Path is /dbname; replace with /new_dbname
    new_path = "/" + new_dbname
    new = parsed._replace(path=new_path)
    return up.urlunparse(new)


def _replace_user_and_dbname(dsn: str, new_user: str, new_password: str, new_dbname: str) -> str:
    """Return a new DSN with user, password, and database replaced."""
    import urllib.parse as up
    parsed = up.urlparse(dsn)
    host = parsed.hostname or "localhost"
    port = parsed.port or 5432
    return f"postgresql://{up.quote(new_user, safe='')}:{up.quote(new_password, safe='')}@{host}:{port}/{new_dbname}"
