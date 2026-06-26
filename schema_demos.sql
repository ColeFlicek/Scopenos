-- Scopenos demos database schema
-- Database: demos
-- Mirrors org_schema.sql but has no user ownership — demo projects are publicly
-- readable. Adds the demo_projects catalog table.
--
-- Usage:
--   psql -d demos -f schema_demos.sql

-- Apply org schema first (identical structure, no duplication)
\i schema_org.sql

-- ── Demo projects catalog ──────────────────────────────────────────────────
-- Registry of demo projects with display metadata. No equivalent in org databases.
CREATE TABLE IF NOT EXISTS demo_projects (
    project_id   TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
    display_name TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    repo_url     TEXT NOT NULL DEFAULT '',
    last_indexed TEXT,
    auto_update  INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL
);
