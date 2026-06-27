-- Scopenos org database schema
-- Database: org_{slug}
-- Applied once per org database at provisioning time, then on every server
-- startup for that org's connection. Safe to re-run — all idempotent.
--
-- Structure:
--   public schema  → org-level tables shared across all projects
--   {project_slug} → per-project tables, created by create_project_schema()
--
-- Usage (provisioning a new org):
--   psql -d org_{slug} -f schema_org.sql
--
-- Usage (server startup — applied automatically by CallGraphDB.init()):
--   The pgvector extension must be pre-created by the provisioner (superuser)
--   before this file is applied. CallGraphDB.init() handles this via a
--   separate bootstrap connection before applying this schema.
--
-- NOTE: CREATE EXTENSION vector is intentionally omitted here — it requires
-- superuser and is handled by the provisioning flow or CallGraphDB.init().

-- ── Users (org-local) ─────────────────────────────────────────────────────
-- Org-local user registry. In production this is a cached subset of
-- scopenos_control.users for the members of this org. In the test environment
-- this is the primary user store.
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    plan       TEXT NOT NULL DEFAULT 'free',
    created_at TEXT NOT NULL
);

-- ── API Keys ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS api_keys (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    key_hash   TEXT NOT NULL UNIQUE,
    name       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    last_used  TEXT,
    revoked_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

-- ── Projects registry ──────────────────────────────────────────────────────
-- One row per project in this org. schema_name is the Postgres schema that
-- holds all per-project tables for this project.
CREATE TABLE IF NOT EXISTS projects (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    root          TEXT NOT NULL DEFAULT '',
    branch        TEXT NOT NULL DEFAULT '',
    head_commit   TEXT NOT NULL DEFAULT '',
    schema_name   TEXT NOT NULL UNIQUE,
    parent_schema TEXT,
    is_fork       BOOLEAN NOT NULL DEFAULT FALSE,
    fork_commit   TEXT,
    created_at    TEXT NOT NULL,
    last_indexed  TEXT,
    node_count    INTEGER NOT NULL DEFAULT 0,
    edge_count    INTEGER NOT NULL DEFAULT 0
);

-- ── Migrations: add new columns to projects before indexes reference them ──
-- Safe to re-run — IF NOT EXISTS on all column additions.
ALTER TABLE projects ADD COLUMN IF NOT EXISTS schema_name   TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS parent_schema TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS is_fork       BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS fork_commit   TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS node_count    INTEGER NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS edge_count    INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_projects_schema ON projects(schema_name);
CREATE INDEX IF NOT EXISTS idx_projects_fork   ON projects(parent_schema) WHERE is_fork = TRUE;

-- ── Project access ─────────────────────────────────────────────────────────
-- Which org members can access which projects. user_id references
-- scopenos_control.users — no FK across databases, stored as TEXT.
CREATE TABLE IF NOT EXISTS project_access (
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    project_id TEXT NOT NULL,
    role       TEXT NOT NULL DEFAULT 'viewer',
    PRIMARY KEY (user_id, project_id)
);

CREATE INDEX IF NOT EXISTS idx_paccess_project ON project_access(project_id);
CREATE INDEX IF NOT EXISTS idx_paccess_user    ON project_access(user_id);

-- ── Contracts ──────────────────────────────────────────────────────────────
-- Invariant contracts scoped to this org. project_ids is a JSON array so
-- one contract can span multiple projects within the org.
-- Dynamic embedding tables (contract_violation_{safe_id}) are created in code
-- when a contract is approved — they live in this public schema alongside contracts.
CREATE TABLE IF NOT EXISTS contracts (
    id                    TEXT PRIMARY KEY,
    project_ids           TEXT NOT NULL DEFAULT '[]',
    function_ids          TEXT NOT NULL DEFAULT '[]',
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

-- ── Pattern prototypes ─────────────────────────────────────────────────────
-- GoF role embedding vectors (Observer, Singleton, etc.). Model-scoped, not
-- project-scoped. Shared across all projects in this org. Per-org rather than
-- global to protect against embedding model version mismatch between orgs.
CREATE TABLE IF NOT EXISTS pattern_prototypes (
    role             TEXT NOT NULL,
    model            TEXT NOT NULL,
    vector           vector(1536),
    description_hash TEXT NOT NULL DEFAULT '',
    computed_at      TEXT NOT NULL,
    PRIMARY KEY (role, model)
);

-- ── Embedding cache ────────────────────────────────────────────────────────
-- Content-addressable cache keyed by body_hash. Shared across all projects in
-- this org to avoid redundant embedding API calls for identical functions.
-- Per-org because orgs may use different embedding models.
CREATE TABLE IF NOT EXISTS embedding_cache (
    body_hash  TEXT PRIMARY KEY,
    embedding  vector(1536) NOT NULL,
    model      TEXT NOT NULL DEFAULT '',
    cached_at  TEXT NOT NULL
);

-- ── Demo project catalog ───────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS demo_projects (
    project_id   TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    description  TEXT NOT NULL DEFAULT '',
    repo_url     TEXT NOT NULL DEFAULT '',
    last_indexed TEXT,
    auto_update  INTEGER NOT NULL DEFAULT 1,
    added_at     TEXT NOT NULL
);

-- ── Public-schema fallback for per-project tables ─────────────────────────
-- These tables mirror the per-project schema template below but live in
-- public so that non-schema-scoped connections (org-level API, test fixtures)
-- can still query them with project_id scoping.
--
-- When a project-scoped connection has search_path TO "{schema}", public
-- these public tables are shadowed by {schema}.nodes etc. — queries go to
-- the project schema first. For backwards-compat and single-DB deployments
-- all data lives here; migration to per-project schemas is Task 12.

CREATE TABLE IF NOT EXISTS nodes (
    project_id       TEXT NOT NULL DEFAULT 'default',
    id               TEXT NOT NULL,
    file             TEXT NOT NULL,
    module           TEXT NOT NULL,
    type             TEXT NOT NULL,
    name             TEXT NOT NULL,
    signature        TEXT NOT NULL DEFAULT '',
    docstring        TEXT NOT NULL DEFAULT '',
    summary          TEXT NOT NULL DEFAULT '',
    body_hash        TEXT NOT NULL DEFAULT '',
    decorators       TEXT NOT NULL DEFAULT '[]',
    embedding_model  TEXT NOT NULL DEFAULT '',
    is_external      INTEGER NOT NULL DEFAULT 0,
    body             TEXT NOT NULL DEFAULT '',
    leading_comment  TEXT NOT NULL DEFAULT '',
    start_line       INT  NOT NULL DEFAULT 0,
    end_line         INT  NOT NULL DEFAULT 0,
    return_type      TEXT NOT NULL DEFAULT '',
    is_async         INTEGER NOT NULL DEFAULT 0,
    parameter_names  TEXT NOT NULL DEFAULT '[]',
    enclosing_class  TEXT NOT NULL DEFAULT '',
    structural_layer TEXT NOT NULL DEFAULT 'precision',
    PRIMARY KEY (project_id, id)
);

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS start_line       INT     NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS end_line         INT     NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS return_type      TEXT    NOT NULL DEFAULT '';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS is_async         INTEGER NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS parameter_names  TEXT    NOT NULL DEFAULT '[]';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS enclosing_class  TEXT    NOT NULL DEFAULT '';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS structural_layer TEXT    NOT NULL DEFAULT 'precision';
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS tsv tsvector
    GENERATED ALWAYS AS (
        setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
        setweight(to_tsvector('english', coalesce(signature, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
        setweight(to_tsvector('english', coalesce(docstring, '')), 'C')
    ) STORED;

CREATE INDEX IF NOT EXISTS idx_nodes_file    ON nodes(file);
CREATE INDEX IF NOT EXISTS idx_nodes_name    ON nodes(name);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_nodes_tsv     ON nodes USING GIN(tsv);

CREATE TABLE IF NOT EXISTS edges (
    id         BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL DEFAULT 'default',
    caller_id  TEXT NOT NULL,
    callee_id  TEXT NOT NULL,
    edge_type  TEXT NOT NULL,
    file       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_edges_caller  ON edges(caller_id);
CREATE INDEX IF NOT EXISTS idx_edges_callee  ON edges(callee_id);
CREATE INDEX IF NOT EXISTS idx_edges_project ON edges(project_id);

CREATE TABLE IF NOT EXISTS function_embeddings (
    id         TEXT NOT NULL,
    project_id TEXT NOT NULL,
    embedding  vector(1536),
    PRIMARY KEY (project_id, id)
);

CREATE INDEX IF NOT EXISTS idx_femb_hnsw ON function_embeddings
    USING hnsw (embedding vector_cosine_ops);

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

CREATE TABLE IF NOT EXISTS decision_embeddings (
    id        TEXT PRIMARY KEY,
    embedding vector(1536)
);

CREATE TABLE IF NOT EXISTS decision_functions (
    decision_id TEXT NOT NULL,
    function_id TEXT NOT NULL,
    PRIMARY KEY (decision_id, function_id)
);

CREATE INDEX IF NOT EXISTS idx_df_function ON decision_functions(function_id);

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

CREATE TABLE IF NOT EXISTS branch_function_changes (
    project_id   TEXT NOT NULL,
    branch       TEXT NOT NULL,
    function_id  TEXT NOT NULL,
    head_commit  TEXT NOT NULL DEFAULT '',
    modified_at  TEXT NOT NULL,
    PRIMARY KEY (project_id, branch, function_id)
);

CREATE INDEX IF NOT EXISTS idx_bfc_project_function ON branch_function_changes(project_id, function_id);
CREATE INDEX IF NOT EXISTS idx_bfc_project_branch   ON branch_function_changes(project_id, branch);

CREATE TABLE IF NOT EXISTS commit_function_changes (
    project_id   TEXT NOT NULL,
    commit_hash  TEXT NOT NULL,
    function_id  TEXT NOT NULL,
    branch       TEXT NOT NULL DEFAULT '',
    changed_at   TEXT NOT NULL,
    PRIMARY KEY (project_id, commit_hash, function_id)
);

CREATE INDEX IF NOT EXISTS idx_cfc_project_function ON commit_function_changes(project_id, function_id);
CREATE INDEX IF NOT EXISTS idx_cfc_project_commit   ON commit_function_changes(project_id, commit_hash);

CREATE TABLE IF NOT EXISTS module_patterns (
    project_id          TEXT NOT NULL,
    module              TEXT NOT NULL,
    naming_regex        TEXT NOT NULL DEFAULT '',
    async_ratio         REAL NOT NULL DEFAULT -1,
    primary_chokepoints TEXT NOT NULL DEFAULT '[]',
    computed_at         TEXT NOT NULL,
    PRIMARY KEY (project_id, module)
);

CREATE TABLE IF NOT EXISTS schema_object_embeddings (
    project_id  TEXT NOT NULL,
    name        TEXT NOT NULL,
    source      TEXT NOT NULL,
    cardinality TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    refs        TEXT NOT NULL DEFAULT '[]',
    refs_in     TEXT NOT NULL DEFAULT '[]',
    embedding   vector(1536),
    PRIMARY KEY (project_id, name, source)
);

-- ── Per-project schema template ────────────────────────────────────────────
-- create_project_schema(p_schema) creates all per-project tables inside the
-- given schema. Call once when a project is first indexed.
-- drop_project_schema(p_schema) tears down a project or fork cleanly.

CREATE OR REPLACE FUNCTION create_project_schema(p_schema TEXT) RETURNS void AS $$
BEGIN
    EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', p_schema);

    -- Nodes (functions and classes)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.nodes (
            project_id       TEXT NOT NULL DEFAULT 'default',
            id               TEXT NOT NULL,
            file             TEXT NOT NULL,
            module           TEXT NOT NULL,
            type             TEXT NOT NULL,
            name             TEXT NOT NULL,
            signature        TEXT NOT NULL DEFAULT '',
            docstring        TEXT NOT NULL DEFAULT '',
            summary          TEXT NOT NULL DEFAULT '',
            body_hash        TEXT NOT NULL DEFAULT '',
            decorators       TEXT NOT NULL DEFAULT '[]',
            embedding_model  TEXT NOT NULL DEFAULT '',
            is_external      INTEGER NOT NULL DEFAULT 0,
            body             TEXT NOT NULL DEFAULT '',
            leading_comment  TEXT NOT NULL DEFAULT '',
            start_line       INT  NOT NULL DEFAULT 0,
            end_line         INT  NOT NULL DEFAULT 0,
            return_type      TEXT NOT NULL DEFAULT '',
            is_async         INTEGER NOT NULL DEFAULT 0,
            parameter_names  TEXT NOT NULL DEFAULT '[]',
            enclosing_class  TEXT NOT NULL DEFAULT '',
            structural_layer TEXT NOT NULL DEFAULT 'precision',
            PRIMARY KEY (project_id, id)
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.nodes(file)',
        p_schema || '_idx_nodes_file', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.nodes(name)',
        p_schema || '_idx_nodes_name', p_schema);
    EXECUTE format($sql$
        ALTER TABLE %I.nodes ADD COLUMN IF NOT EXISTS tsv tsvector
            GENERATED ALWAYS AS (
                setweight(to_tsvector('english', coalesce(name, '')), 'A') ||
                setweight(to_tsvector('english', coalesce(signature, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(summary, '')), 'B') ||
                setweight(to_tsvector('english', coalesce(docstring, '')), 'C')
            ) STORED
    $sql$, p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.nodes USING GIN(tsv)',
        p_schema || '_idx_nodes_tsv', p_schema);

    -- Edges (call graph)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.edges (
            id         BIGSERIAL PRIMARY KEY,
            project_id TEXT NOT NULL DEFAULT 'default',
            caller_id  TEXT NOT NULL,
            callee_id  TEXT NOT NULL,
            edge_type  TEXT NOT NULL,
            file       TEXT NOT NULL
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.edges(caller_id)',
        p_schema || '_idx_edges_caller', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.edges(callee_id)',
        p_schema || '_idx_edges_callee', p_schema);

    -- Function embeddings (HNSW index for ANN search)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.function_embeddings (
            id         TEXT NOT NULL,
            project_id TEXT NOT NULL,
            embedding  vector(1536),
            PRIMARY KEY (project_id, id)
        )
    $sql$, p_schema);

    EXECUTE format($sql$
        CREATE INDEX IF NOT EXISTS %I ON %I.function_embeddings
        USING hnsw (embedding vector_cosine_ops)
    $sql$, p_schema || '_idx_femb_hnsw', p_schema);

    -- Decisions
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.decisions (
            id                    TEXT PRIMARY KEY,
            project_id            TEXT NOT NULL DEFAULT 'default',
            type                  TEXT NOT NULL,
            description           TEXT NOT NULL,
            rejected_alternatives TEXT NOT NULL DEFAULT '',
            trigger               TEXT NOT NULL DEFAULT '',
            parent_decision_id    TEXT,
            created_at            TEXT NOT NULL
        )
    $sql$, p_schema);

    -- Decision embeddings (UUID-keyed, scoped to this project's decisions)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.decision_embeddings (
            id        TEXT PRIMARY KEY,
            embedding vector(1536)
        )
    $sql$, p_schema);

    -- Decision → function join table
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.decision_functions (
            decision_id TEXT NOT NULL,
            function_id TEXT NOT NULL,
            PRIMARY KEY (decision_id, function_id)
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.decision_functions(function_id)',
        p_schema || '_idx_df_function', p_schema);

    -- Agent improvements
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.agent_improvements (
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
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.agent_improvements(status)',
        p_schema || '_idx_impr_status', p_schema);

    -- Project home snapshots
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.project_home_snapshots (
            project_id  TEXT PRIMARY KEY,
            hashes      TEXT NOT NULL DEFAULT '{}',
            captured_at TEXT NOT NULL
        )
    $sql$, p_schema);

    -- Dependency fingerprints
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.dependency_fingerprints (
            id               TEXT PRIMARY KEY,
            project_id       TEXT NOT NULL,
            captured_at      TEXT NOT NULL,
            fingerprint_hash TEXT NOT NULL,
            snapshot_json    TEXT NOT NULL,
            diff_json        TEXT
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.dependency_fingerprints(project_id, captured_at)',
        p_schema || '_idx_depfp', p_schema);

    -- Branch function changes (latest touch per branch/function)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.branch_function_changes (
            project_id  TEXT NOT NULL,
            branch      TEXT NOT NULL,
            function_id TEXT NOT NULL,
            head_commit TEXT NOT NULL DEFAULT '',
            modified_at TEXT NOT NULL,
            PRIMARY KEY (project_id, branch, function_id)
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.branch_function_changes(project_id, function_id)',
        p_schema || '_idx_bfc_fn', p_schema);

    -- Commit function changes (append-only full history)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.commit_function_changes (
            project_id  TEXT NOT NULL,
            commit_hash TEXT NOT NULL,
            function_id TEXT NOT NULL,
            branch      TEXT NOT NULL DEFAULT '',
            changed_at  TEXT NOT NULL,
            PRIMARY KEY (project_id, commit_hash, function_id)
        )
    $sql$, p_schema);

    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.commit_function_changes(project_id, function_id)',
        p_schema || '_idx_cfc_fn', p_schema);
    EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.commit_function_changes(project_id, commit_hash)',
        p_schema || '_idx_cfc_commit', p_schema);

    -- Module patterns
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.module_patterns (
            project_id          TEXT NOT NULL,
            module              TEXT NOT NULL,
            naming_regex        TEXT NOT NULL DEFAULT '',
            async_ratio         REAL NOT NULL DEFAULT -1,
            primary_chokepoints TEXT NOT NULL DEFAULT '[]',
            computed_at         TEXT NOT NULL,
            PRIMARY KEY (project_id, module)
        )
    $sql$, p_schema);

    -- Schema object embeddings (DB tables and class embeddings)
    EXECUTE format($sql$
        CREATE TABLE IF NOT EXISTS %I.schema_object_embeddings (
            project_id  TEXT NOT NULL,
            name        TEXT NOT NULL,
            source      TEXT NOT NULL,
            cardinality TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            refs        TEXT NOT NULL DEFAULT '[]',
            refs_in     TEXT NOT NULL DEFAULT '[]',
            embedding   vector(1536),
            PRIMARY KEY (project_id, name, source)
        )
    $sql$, p_schema);

END;
$$ LANGUAGE plpgsql;


-- drop_project_schema tears down a project or fork and removes its projects row.
CREATE OR REPLACE FUNCTION drop_project_schema(p_schema TEXT) RETURNS void AS $$
BEGIN
    -- Safety check: refuse to drop public or system schemas
    IF p_schema IN ('public', 'pg_catalog', 'information_schema', 'pg_toast') THEN
        RAISE EXCEPTION 'Cannot drop system schema: %', p_schema;
    END IF;

    EXECUTE format('DROP SCHEMA IF EXISTS %I CASCADE', p_schema);
    DELETE FROM projects WHERE schema_name = p_schema;
END;
$$ LANGUAGE plpgsql;
