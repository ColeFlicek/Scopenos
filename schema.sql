-- ACIP Postgres schema
-- Run once per database: psql -d acip -f schema.sql
-- pgvector extension is created here; requires superuser (POSTGRES_USER is superuser in Docker/K8s).
CREATE EXTENSION IF NOT EXISTS vector;
-- In local dev, run: scripts/setup_db.sh

CREATE TABLE IF NOT EXISTS projects (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    root         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    last_indexed TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    project_id      TEXT NOT NULL DEFAULT 'default',
    id              TEXT NOT NULL,
    file            TEXT NOT NULL,
    module          TEXT NOT NULL,
    type            TEXT NOT NULL,
    name            TEXT NOT NULL,
    signature       TEXT NOT NULL DEFAULT '',
    docstring       TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    body_hash       TEXT NOT NULL DEFAULT '',
    decorators      TEXT NOT NULL DEFAULT '[]',
    embedding_model TEXT NOT NULL DEFAULT '',
    is_external     INTEGER NOT NULL DEFAULT 0,
    body            TEXT NOT NULL DEFAULT '',
    leading_comment TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (project_id, id)
);

CREATE TABLE IF NOT EXISTS edges (
    id          BIGSERIAL PRIMARY KEY,
    project_id  TEXT NOT NULL DEFAULT 'default',
    caller_id   TEXT NOT NULL,
    callee_id   TEXT NOT NULL,
    edge_type   TEXT NOT NULL,
    file        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_caller ON edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee ON edges(callee_id);
CREATE INDEX IF NOT EXISTS idx_nodes_file   ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name   ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_edges_project ON edges(project_id);

-- Unified function embeddings table (replaces per-project vec0 tables).
-- One row per (project_id, function_id). HNSW index for fast ANN search.
CREATE TABLE IF NOT EXISTS function_embeddings (
    id         TEXT NOT NULL,
    project_id TEXT NOT NULL,
    embedding  vector(1536),
    PRIMARY KEY (project_id, id)
);

CREATE INDEX IF NOT EXISTS idx_femb_hnsw ON function_embeddings
    USING hnsw (embedding vector_cosine_ops);

-- Decision embeddings (shared across all projects — UUIDs, no collision risk)
CREATE TABLE IF NOT EXISTS decision_embeddings (
    id        TEXT PRIMARY KEY,
    embedding vector(1536)
);

CREATE TABLE IF NOT EXISTS decisions (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL DEFAULT 'default',
    type                  TEXT NOT NULL,
    description           TEXT NOT NULL,
    rejected_alternatives TEXT NOT NULL DEFAULT '',
    trigger               TEXT NOT NULL DEFAULT '',
    parent_decision_id    TEXT,
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS decision_functions (
    decision_id TEXT NOT NULL,
    function_id TEXT NOT NULL,
    PRIMARY KEY (decision_id, function_id)
);

CREATE INDEX IF NOT EXISTS idx_df_function ON decision_functions(function_id);

CREATE TABLE IF NOT EXISTS contracts (
    id                    TEXT PRIMARY KEY,
    project_ids           TEXT NOT NULL DEFAULT '[]',
    title                 TEXT NOT NULL,
    natural_language      TEXT NOT NULL,
    rule_type             TEXT NOT NULL DEFAULT 'SEMANTIC',
    structural_expression TEXT NOT NULL DEFAULT '{}',
    threshold             DOUBLE PRECISION NOT NULL DEFAULT 0.85,
    status                TEXT NOT NULL DEFAULT 'draft',
    created_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contract_examples (
    id           TEXT PRIMARY KEY,
    contract_id  TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    example_type TEXT NOT NULL,
    code         TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cex_contract ON contract_examples(contract_id);

-- Contract embedding tables (one pair per contract, created dynamically)
-- Naming: contract_violation_{safe_id}, contract_compliance_{safe_id}
-- Created in code when a contract is approved.

CREATE TABLE IF NOT EXISTS contract_violations (
    id             TEXT PRIMARY KEY,
    contract_id    TEXT NOT NULL REFERENCES contracts(id) ON DELETE CASCADE,
    function_id    TEXT NOT NULL,
    project_id     TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    score          DOUBLE PRECISION NOT NULL DEFAULT 0.0,
    detected_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cviol_contract  ON contract_violations(contract_id);
CREATE INDEX IF NOT EXISTS idx_cviol_project   ON contract_violations(project_id);
CREATE INDEX IF NOT EXISTS idx_cviol_function  ON contract_violations(function_id);

CREATE TABLE IF NOT EXISTS agent_improvements (
    id                 TEXT PRIMARY KEY,
    project_id         TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL,
    description        TEXT NOT NULL,
    affected_functions TEXT NOT NULL DEFAULT '[]',
    severity           TEXT NOT NULL DEFAULT 'medium',
    suggested_fix      TEXT NOT NULL DEFAULT '',
    reproduction_steps TEXT NOT NULL DEFAULT '',
    status             TEXT NOT NULL DEFAULT 'open',
    filed_at           TEXT NOT NULL,
    resolved_at        TEXT,
    resolution_notes   TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_impr_project ON agent_improvements(project_id);
CREATE INDEX IF NOT EXISTS idx_impr_status  ON agent_improvements(status);

CREATE TABLE IF NOT EXISTS project_home_snapshots (
    project_id  TEXT PRIMARY KEY,
    hashes      TEXT NOT NULL DEFAULT '{}',
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dependency_fingerprints (
    id               TEXT PRIMARY KEY,
    project_id       TEXT NOT NULL,
    captured_at      TEXT NOT NULL,
    fingerprint_hash TEXT NOT NULL,
    snapshot_json    TEXT NOT NULL,
    diff_json        TEXT
);

CREATE INDEX IF NOT EXISTS idx_depfp_project ON dependency_fingerprints(project_id, captured_at);

CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    plan       TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash   TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used  TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS project_access (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'viewer',
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_paccess_project ON project_access(project_id);

CREATE TABLE IF NOT EXISTS demo_projects (
    project_id   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    repo_url     TEXT NOT NULL DEFAULT '',
    last_indexed TEXT,
    auto_update  INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL
);
