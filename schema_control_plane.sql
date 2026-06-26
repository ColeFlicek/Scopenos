-- Scopenos control plane schema
-- Database: scopenos_control
-- Applied once at cluster setup. Run as superuser or DB owner.
-- Safe to re-run — all statements are idempotent.
--
-- Usage:
--   psql -d scopenos_control -f schema_control_plane.sql

CREATE EXTENSION IF NOT EXISTS vector;

-- ── Organizations ──────────────────────────────────────────────────────────
-- One row per customer org. Provisioned by scopenos_provisioner role at signup.
-- db_url stores the org's database connection string (encrypted at app layer).
CREATE TABLE IF NOT EXISTS organizations (
    id         TEXT PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,
    db_url     TEXT NOT NULL DEFAULT '',
    plan       TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL,
    updated_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_orgs_slug ON organizations(slug);

-- ── Users ──────────────────────────────────────────────────────────────────
-- Global user accounts. One user can belong to multiple orgs.
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

-- ── API Keys ───────────────────────────────────────────────────────────────
-- Global API keys. Each key maps to a user + org.
-- org_id is nullable to support admin keys not scoped to an org.
CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     TEXT REFERENCES organizations(id) ON DELETE CASCADE,
    key_hash   TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used  TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_api_keys_org  ON api_keys(org_id);

-- ── Org Members ────────────────────────────────────────────────────────────
-- Which users belong to which orgs. Role is the org-level role (owner, member).
CREATE TABLE IF NOT EXISTS org_members (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    org_id     TEXT NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'member',
    joined_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, org_id)
);

CREATE INDEX IF NOT EXISTS idx_org_members_org  ON org_members(org_id);
CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(user_id);
