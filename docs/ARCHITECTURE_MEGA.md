# Scopenos — Master Architecture Reference

> **Scope:** Single source of truth for Scopenos architecture, infrastructure, tooling, operations, limitations, business context, and roadmap. Consolidates all internal docs, session learnings, and post-migration knowledge. Last updated: 2026-07-02.

> **Migration note:** The `WHITEPAPER.md` still describes the old SQLite + sqlite-vec single-container architecture (pre-Phase 11). The current system uses **PostgreSQL with pgvector**, schema-per-project isolation, and multi-tenancy via per-org databases. Where the whitepaper contradicts this document, this document is correct.

> **Naming history:** ACIP (Agentic Coding Intelligence Platform) → Phronosis → Actio → **Scopenos** (current). Legacy artifacts: `/workspace/ACIP` directory path, `WHITEPAPER.md`/`BUSINESS.md` titles, `~/.claude/CLAUDE.md` ("Phronosis" section), `acip` database name in old health reports. The `acip` database from `docs/db_health.md` is the pre-Phase 11 SQLite-era database — current data lives in `org_scopenos`. Any reference to "Phronosis", "Actio", or "ACIP" in config files or docs is a legacy artifact.

---

## Table of Contents

1. [What Scopenos Is](#1-what-scopenos-is)
2. [System Architecture Overview](#2-system-architecture-overview)
3. [Multi-Tenant Data Model](#3-multi-tenant-data-model)
4. [Infrastructure Topology](#4-infrastructure-topology)
5. [Request Lifecycle](#5-request-lifecycle)
6. [Indexing Pipeline](#6-indexing-pipeline)
7. [Fork Infrastructure](#7-fork-infrastructure)
8. [MCP Tool Reference](#8-mcp-tool-reference)
9. [Guidance Layer](#9-guidance-layer)
10. [Contract Enforcement](#10-contract-enforcement)
11. [Session Identity System](#11-session-identity-system)
12. [Kubernetes & Operations](#12-kubernetes--operations)
13. [Demo Repos](#13-demo-repos)
14. [Known Limitations](#14-known-limitations)
15. [Phase 16 Audit](#15-phase-16-audit)
16. [Architecture Deepening Decisions](#16-architecture-deepening-decisions)
17. [Business Case & Commercialization](#17-business-case--commercialization)
18. [Roadmap](#18-roadmap)

---

## 1. What Scopenos Is

Scopenos is a self-hosted code intelligence server. It indexes a codebase into four complementary knowledge stores and exposes them via MCP (Model Context Protocol) to Claude Code sessions.

**The core problem it solves:** AI coding assistants that understand a codebase by reading files are slow, expensive, and incomplete. Scopenos replaces 10 file reads with 1 MCP call. A query like "what calls this function?" gets a precise, pre-computed answer from a graph database instead of scanning source files.

### What Scopenos Actually Sells

1. **Persistent organizational memory** — Decision history that survives team turnover, context window limits, and agent session boundaries. Once a team has 18 months of decision history, that institutional knowledge belongs to the organization, not any individual or AI session.

2. **Token economics** — At the rate AI coding is scaling, replacing exploratory file reads with structured MCP queries becomes a meaningful cost reduction and quality improvement.

3. **Multi-agent coordination substrate** — As AI coding scales beyond one agent per developer, teams need a shared nervous system where agent A's work is visible to agent B before B touches the same code. Scopenos is that layer at the MCP protocol level.

4. **Governance layer (Invariant Contracts)** — The contract enforcement system is a compliance/audit product with an independent enterprise sales story.

### What Scopenos Is Not

- **Not a language server** — No completions, hover, or go-to-definition. It provides graph traversal and semantic search at a higher abstraction level.
- **Not real-time** — Index updated on commit (via hook) or manually. No filesystem watcher.
- **Not a code reviewer** — It surfaces structure and history; Claude Code does the reasoning.

---

## 2. System Architecture Overview

```
┌──────────────────────────────────────────────────────────────────────┐
│  Claude Code (developer's machine or container)                       │
│  MCP Client → calls tools via StreamableHTTP                         │
└───────────────────────────┬──────────────────────────────────────────┘
                            │  HTTP (MCP protocol + REST)
                            │  port 3004 (via Tailscale VPN)
                            ▼
┌──────────────────────────────────────────────────────────────────────┐
│  ACIP Server  (K3d pod on TheHive, 100.71.88.106)                    │
│  FastMCP + Starlette + ASGI                                          │
│                                                                      │
│  ┌─────────────────┐  AuthMiddleware (ASGI)                          │
│  │ OrgRouter       │  → resolves X-API-Key to (user, OrgDB)         │
│  │ per-org         │                                                 │
│  │ CallGraphDB     │  MCP tools + REST endpoints                     │
│  │ pool cache      │                                                 │
│  └─────────────────┘                                                 │
│                                                                      │
│  ┌────────────┐  ┌──────────────────┐  ┌───────────────────────┐   │
│  │ Layer 1    │  │ Layer 2          │  │ Layer 3               │   │
│  │ Call Graph │  │ Embeddings       │  │ Decision Memory       │   │
│  │ (Postgres) │  │ (pgvector)       │  │ (Postgres + pgvector) │   │
│  └────────────┘  └──────────────────┘  └───────────────────────┘   │
│                                                                      │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │ Layer 4: Guidance Layer (in-process, zero-latency signals)   │   │
│  └──────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
        │                               │
        │ Postgres connections           │ Embedding API calls
        ▼                               ▼
┌──────────────────┐          ┌─────────────────────┐
│  Postgres        │          │  OpenAI / Ollama    │
│  (Docker on      │          │  (text-embedding-   │
│  TheHive host,   │          │   3-small default)  │
│  172.21.0.1)     │          └─────────────────────┘
│                  │
│  scopenos        │  ← control plane DB (users, api_keys, orgs)
│  org_<slug>      │  ← one database per customer org
│    schema_<proj> │  ← one schema per project within org DB
└──────────────────┘
```

**Deployment:** Single K3d pod on TheHive home server. Accessible over Tailscale VPN. Postgres runs as a Docker container on the host (not in K3s) so indexed data survives cluster recreation. Redis is in-cluster (stateless).

---

## 3. Multi-Tenant Data Model

### All Postgres databases

| Database | Purpose | Accessed by |
|---|---|---|
| `scopenos` | Control plane — orgs, users, API keys | `scopenos_control_rw` |
| `org_scopenos` | Scopenos org's project data | `org_scopenos_rw` |
| `org_{slug}` | One per customer org | `org_{slug}_rw` (auto-created at provision) |
| `scopenos_test` | Test suite only — never production data | `scopenos_test_runner` |
| `template_vector` | Template DB with pgvector pre-installed — cloned when provisioning new org DBs | `scopenos` (superuser) |
| `org_demos` | Demo repos — 12 SWE-bench repos pre-indexed (django 42K nodes, pytest 8K, etc.) | `scopenos_demos_reader` (RO), `scopenos_demos_writer` (RW) |
| `org_benchmark` | Benchmark org — 4 demo repos mirrored from org_demos for SWE-bench forks. Accessible via `BENCH_API_KEY`. Pre-seeded schemas (django, pytest, flask, requests) needed ownership transfer after pg_dump copy (done 2026-07-03). | `org_benchmark_rw` |

### Three levels of isolation

```
Control plane DB (scopenos)
  └── users, api_keys, organizations, org_database_urls
      └── resolves api_key → org_id → org_db_url

Org database (org_<slug>)  [one Postgres database per customer org]
  └── public schema: projects, project_access
      └── project schema (<proj>): nodes, edges, function_embeddings,
                                   decisions, decision_embeddings,
                                   contracts, improvements, ...
          └── fork schema (<proj>_fork_<commit7>): copy of project schema
                                                    + delta applied
```

### Control plane DB

**DB name: `scopenos`** — NOT `scopenos_control`. Any doc or memory file that says `scopenos_control` as a DB name is wrong (naming correction confirmed 2026-07-01).

Holds the auth and routing tables. Roles:

| Table | Purpose |
|---|---|
| `users` | Registered users with `is_admin` flag |
| `api_keys` | Hashed keys → user_id + org_id |
| `organizations` | org_id → slug + `db_url` (DSN for that org's database) |

No project data lives here — it's routing infrastructure only.

**How org DB URLs work:** There is NO static `ORG_DB_URL` secret. The org's connection string is stored in `scopenos.organizations.db_url`. The API pods resolve it at runtime: `CONTROL_DB_URL` → connect to `scopenos` DB → look up org by API key → fetch `db_url`.

```bash
# Get all org DB URLs (from TheHive)
docker exec -it acip-postgres psql -U scopenos scopenos \
  -c "SELECT slug, db_url FROM organizations;"
```

### Org database (`org_<slug>`)

One Postgres database per customer organization. The database is provisioned by `provision_org.py` which:
1. Creates the database as the `scopenos_provisioner` role
2. Creates an org-specific read/write role (`<slug>_rw`)
3. Applies the org schema template (`schema_org.sql`)
4. Grants all privileges needed for `create_project_schema` to work
5. Registers the DSN in the control plane's `org_database_urls` table

**Critical grants that manual provisioning misses** (these caused the org_benchmark debugging session):
```sql
GRANT CONNECT, CREATE ON DATABASE "org_benchmark" TO "org_benchmark_rw";
GRANT USAGE ON SCHEMA public TO "org_benchmark_rw";
GRANT ALL ON ALL TABLES IN SCHEMA public TO "org_benchmark_rw";
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO "org_benchmark_rw";
```

### Schema-per-project isolation

Within each org database, every indexed project gets its own Postgres schema created by the `create_project_schema($1)` PL/pgSQL function. The function is SECURITY INVOKER (not SECURITY DEFINER), so the caller must have CREATE ON DATABASE.

Schema naming: `derive_schema_name(project_id)` — sanitizes the project slug to a valid Postgres identifier (lowercase, underscore for non-alnum chars, max 40 chars, prefixed with `p_` if starts with digit).

Fork schemas: `{parent_schema}_fork_{commit[:7]}` — e.g., `django_fork_abc1234`.

Tables in each project schema:
```
nodes                   — function/class/method nodes
edges                   — call, inheritance, import edges
function_embeddings     — pgvector table for KNN search
decisions               — architectural decision records
decision_embeddings     — pgvector for semantic decision search
decision_functions      — decisions ↔ functions join table
contracts               — invariant contract rules
contract_violations     — violation records
contract_examples       — few-shot examples per contract
improvements            — agent-filed improvement suggestions
module_patterns         — cached module convention signals
schema_object_embeddings — DB schema object vectors
branch_function_changes  — function-level git change tracking
project_home_snapshots  — cached get_project_home results
```

### `CallGraphDB` and connection pools

`CallGraphDB.create(db_url, skip_schema_init)` creates an asyncpg connection pool. When an org-level pool is created, it's stored in `OrgRouter._pools[org_id]`.

`CallGraphDB.project_db(project_id)` creates a project-scoped pool with `SET search_path TO "{schema}", public`. These are cached in `_project_dbs`.

All pools use `command_timeout=30.0`. All calls in `fork_schema()` use explicit `timeout=300.0` to override the pool default for bulk copy operations.

### OrgRouter

```python
class OrgRouter:
    _pools: dict[str, CallGraphDB]   # org_id → org-level DB pool
    _lock: asyncio.Lock              # one lock per org for lazy init
    control_db: CallGraphDB          # scopenos control plane

    async def resolve_request(api_key, endpoint) -> (user, org_db):
        user = await control_db.get_user_by_key(api_key)
        org_id = user["org_id"]
        org_db = await _get_org_db(org_id)
        return user, org_db

    async def _get_org_db(org_id) -> CallGraphDB:
        # double-checked locking pattern
        if org_id in _pools: return _pools[org_id]
        async with _lock:
            if org_id in _pools: return _pools[org_id]
            db_url = await control_db.get_org_db_url(org_id)
            org_db = await CallGraphDB.create(db_url, skip_schema_init=True)
            _pools[org_id] = org_db
            return org_db
```

Pools are cleared on pod restart (in-memory cache only). First request per org after restart pays the connection setup latency.

---

## 4. Infrastructure Topology

### TheHive (home server)

- **IP:** 100.71.88.106 (Tailscale VPN)
- **OS:** Unraid (Slackware-based) — **no `apt`, no `pip`, no Python on the host**. Do not try to install Python on TheHive directly. All scripting must go through `docker exec` into a container (e.g. `acip-postgres` for psql) or the sandbox container (has Python + uv, but cannot `docker exec` or `kubectl`).
- **K3d cluster:** `scopenos` — single-node k3d (K3s in Docker)
- **Port mapping:** Host port 3004 → k3d proxy container → cluster port 80 → Traefik ingress → `scopenos-api-svc:3004`

### Network path for inbound requests

```
Client (Tailscale)
  └─► 100.71.88.106:3004     (TheHive host)
       └─► k3d-proxy container (nginx Docker container)
            └─► k3d cluster port 80
                 └─► Traefik ingress (routes "/" to scopenos-api-svc:3004)
                      └─► ClusterIP scopenos-api-svc:3004
                           └─► Pod (scopenos-api)
```

NodePort 30040 is only accessible inside the k3d network (not from host). Use port 3004 (the k3d proxy) for all external access.

### This container (sandbox)

- **IP:** 100.90.90.27 (Tailscale)
- **Type:** Sandbox container spawned by Agent of Empires
- **DB access:** Postgres at 172.21.0.1 via Docker bridge (the host's Docker bridge interface)
- **No psql binary** — use `uv run python -c` with psycopg2 for DB queries from this container
- **No gh binary** by default — install via `apt install gh` or GitHub releases

### Postgres (Docker, not K3s)

Container name: **`acip-postgres`**

```bash
docker run -d --name acip-postgres \
  -e POSTGRES_USER=scopenos \
  -e POSTGRES_DB=scopenos \
  -p 5432:5432 \
  -v scopenos-pgdata:/var/lib/postgresql/data \
  pgvector/pgvector:pg17
```

Runs on TheHive host, not inside K3d. Data persists in named Docker volume across cluster recreation. Reachable at 172.21.0.1:5432 from K3d pods. Superuser is `scopenos` (not the default `postgres`).

```bash
# Exec in (from TheHive)
docker exec -it acip-postgres psql -U scopenos <dbname>
```

**Port on TheHive host:** Unknown — check with `docker inspect acip-postgres | grep HostPort`.

### K3d cluster

```bash
k3d cluster create scopenos \
  --port 3004:80@loadbalancer \
  --k3s-arg "--tls-san=<server-ip>@server:0"
```

**GitHub Actions CI secrets:**

| Secret | Value |
|---|---|
| `KUBECONFIG` | base64 of `k3d kubeconfig get scopenos` |
| `DATABASE_URL` | `postgresql://scopenos:<pw>@172.21.0.1/scopenos` |
| `OPENAI_API_KEY` | For embeddings |
| `ANTHROPIC_API_KEY` | For LLM enrichment |
| `TS_OAUTH_CLIENT_ID` / `TS_OAUTH_SECRET` | Tailscale OAuth for CI access |

### Postgres roles

| Role | CREATEDB | Can reach | Used by |
|---|---|---|---|
| `scopenos_provisioner` | ✓ | All (superuser equiv) | Org creation only. Credentials not in Claude memory. |
| `scopenos_control_rw` | ✗ | `scopenos` DB (RW) | API pods, `CONTROL_DB_URL` secret |
| `org_{slug}_rw` | ✗ | `org_{slug}` only (RW) | API pods at runtime, fetched from control DB |
| `scopenos_demos_writer` | ✗ | `demos` (RW) | Demo indexing jobs |
| `scopenos_demos_reader` | ✗ | `demos` (RO) | Test suite reading demo data |
| `scopenos_test_runner` | ✗ | `scopenos_test` (RW) | pytest in sandbox; password in `/root/.pgpass` on sandbox |

### pgvector extension

Must be installed in each org database:
```sql
CREATE EXTENSION IF NOT EXISTS vector;
```

`register_vector` is called as an asyncpg pool `init` callback for every connection. If the extension is missing, this callback raises, propagating through `_get_org_db` → `AuthMiddleware` → `ServerErrorMiddleware` (plain text "Internal Server Error" response). Fix: install extension before provisioning the org.

---

## 5. Request Lifecycle

### Auth flow

```
POST /api/fork-from-files
  │
  ▼ AuthMiddleware.__call__()
  │  headers → X-API-Key
  │  OrgRouter.resolve_request(key, endpoint)
  │    → control_db.get_user_by_key(key) → user dict
  │    → _get_org_db(user["org_id"]) → org-level CallGraphDB
  │  sets ContextVar: _current_user, _current_org_db
  │  try: await self._app(...)
  │  finally: reset ContextVars
  ▼
  Route handler
    require_user() → reads _current_user ContextVar
    get_current_org_db() → reads _current_org_db ContextVar
    check_permission(user, project_id, "write", org_db)
```

`AuthMiddleware` wraps `resolve_request` in try/except — exceptions are logged but leave `user=None`, `org_db=None`. Downstream handlers then raise 401/503 via `require_user()`.

### MCP tool dispatch

MCP tools call `get_current_org_db()` to get the org-level DB, then use `org_db.project_db(project_id)` for project-scoped queries. The `resolve_project_db` helper in `src/tools/_shared.py` handles the common case of finding a project and returning its scoped DB.

### Exception handling

`POST /api/fork-from-files` and similar endpoints catch non-HTTP exceptions:
```python
except HTTPException:
    raise
except Exception as exc:
    detail = str(exc) or f"{type(exc).__name__}: (no message)"
    print(f"[fork-from-files 500] {type(exc).__name__}: {exc!r}\n{traceback.format_exc()}", flush=True)
    return JSONResponse({"status": "error", "detail": detail}, status_code=500)
```

The `or` fallback matters for `TimeoutError` where `str(exc)` is `""`.

---

## 6. Indexing Pipeline

### Layer 1 — Call Graph

1. **Parsing** (`src/call_graph/parser.py`) — tree-sitter parses Python and TypeScript into ASTs. Two visitors emit:
   - `FunctionNode`: id (module-qualified dotted path), name, file, type, signature, docstring, body (truncated at 2000 chars), `body_hash` (SHA-256[:16] of full function text)
   - `CallEdge`: caller_id, callee_name (unresolved), edge_type (calls/inherits/imports)

2. **ID scheme** — `src.call_graph.storage.CallGraphDB.upsert_nodes` — module-relative dotted path derived by stripping `project_root` and converting path separators to dots.

3. **Storage** (`src/call_graph/storage.py`) — `nodes` table composite PK `(project_id, id)` (in project schema, this means just `id` since the schema is scoped). `_resolve_callee()`: exact match → suffix match → stores bare name for unresolved externals.

4. **Body hash** — SHA-256[:16] of full function text. Only changed functions re-embed on re-index. This is the core of incremental indexing.

### Layer 2 — Semantic Embeddings

1. **Chunking** (`src/embeddings/chunker.py`) — Formats: `Function: {id}\nSignature: {sig}\nDocstring: {doc}\nSummary: {summary}\nBody:\n{body[:2000]}`

2. **Summarization** — One-sentence Claude Haiku summary per function before embedding. Summaries cached in `nodes.summary` and reused on re-index.

3. **Embedding** (`src/embeddings/embedder.py`) — OpenAI `text-embedding-3-small` by default (swappable to local Ollama). Batches of 100 per API call.

4. **Storage** — pgvector `function_embeddings` table with `USING ivfflat` or `USING hnsw` index. (HNSW index not yet applied — see Known Limitations.)

5. **Routing** (`src/embeddings/pipeline.py`) — `EmbeddingPipeline` owns routing strategy: documented functions use small model, undocumented fall back to large model.

### Incremental update flow

```
file changes → parse new nodes → compare body_hash vs. stored hash
→ delete_by_ids(changed | deleted)
→ upsert_nodes → upsert_edges
→ upsert_chunks(changed_ids only) → LLM summary → embed → store
```

Only changed functions touch the embedding API. A one-line fix to a 100-function file embeds one function, not 100.

### Layer 3 — Decision Memory

- `decisions` table: id, type, description, rejected_alternatives, trigger, created_at
- `decision_functions`: many-to-many join to function IDs
- `decision_embeddings`: pgvector for semantic search
- **Log path:** `log_decision()` → generate reasoning text → embed → write structured record → link function IDs
- **Get path:** `get_decision_history(fn)` — SQL lookup, no embeddings
- **Semantic query:** `search_decisions(text)` — embed → KNN over `decision_embeddings` → fetch records

Auto-population: post-commit git hook calls `/api/decisions` on every commit, linking decisions to changed function IDs via `/api/functions` endpoint.

### Layer 4 — Guidance Layer

In-process signals injected into MCP tool responses. Zero additional latency (pure logic or 1–3 targeted DB queries in parallel). See [Section 9](#9-guidance-layer) for full signal reference.

### Key architectural components extracted from `CallGraphDB`

Per `docs/architecture-deepening.md` — five major extractions:

| New class | Extracted from | Reason |
|---|---|---|
| `ArchitectureAnalyzer` (`src/analysis.py`) | `CallGraphDB.get_project_home_data()` | Analytics != storage |
| `GraphData`, `ArchitectureSnapshot` (`src/call_graph/models.py`) | Inline dicts | Type-safe data contract |
| `EmbeddingPipeline` (`src/embeddings/pipeline.py`) | `EmbeddingStore.upsert_chunks()` | Routing strategy != storage |
| `ContractRule` (`src/contracts/rule.py`) | `ContractManager._check_structural()` | Pure rule logic != async I/O |
| `Services` dataclass (`src/server.py`) | `_services: dict[str, Any]` | Type safety at handler injection point |

---

## 7. Fork Infrastructure

### Purpose

Create isolated project snapshots at a specific git commit without re-indexing from scratch. Used by the SWE-bench benchmark to test code comprehension at the exact state of a task's test commit.

### `create_fork_from_files` (client-side delta)

```python
async def create_fork_from_files(
    parent_project_id, fork_project_id, files, project_root, org_db, user_id, target_commit
) -> dict
```

1. Check if fork already exists (`get_schema_name_for_project(fork_project_id, fallback=False)`)
2. Get parent schema name (fallback=False — None means no project row, i.e., not indexed)
3. Derive fork schema name: `{parent_schema}_fork_{commit[:7]}`
4. `fork_schema(parent_schema, fork_schema_name)` — copies structural tables
5. `apply_fork_delta_from_files(...)` — applies file content delta
6. `insert_fork_project(...)` — registers fork in `public.projects`
7. `grant_project_access(user_id, fork_project_id, "owner")` if user_id provided

### `fork_schema` — bulk copy

```python
_FORK_TIMEOUT = 300.0

async with self._pool.acquire() as conn:
    # Drop any orphan schema left by a previous failed fork attempt
    await conn.execute("SELECT drop_project_schema($1)", fork_schema_name, timeout=_FORK_TIMEOUT)
    await conn.execute("SELECT create_project_schema($1)", fork_schema_name, timeout=_FORK_TIMEOUT)
    for table in _copy_tables:
        # INSERT INTO "{fork_schema}".{table} SELECT ... FROM "{parent_schema}".{table}
        await conn.execute(..., timeout=_FORK_TIMEOUT)
```

The orphan drop is critical: a failed previous fork attempt leaves a schema with data but no `projects` row. Without the drop, the next attempt hits duplicate primary key errors. This is safe because `create_fork_from_files` only reaches `fork_schema` after confirming no project row exists.

**Tables copied:** nodes, edges, function_embeddings, decisions, decision_embeddings, decision_functions, contracts, contract_violations, contract_examples, agent_improvements. The `project_home_snapshots` table is skipped (regenerated on first `get_project_home` call).

### `fallback` parameter on `get_schema_name_for_project`

```python
async def get_schema_name_for_project(project_id, fallback: bool = True) -> str | None:
```

- `fallback=True` (default): returns a derived schema name even if no project row exists — used by tools that need to look up schema without caring if project is registered
- `fallback=False`: returns `None` if no project row exists — used by fork infrastructure to distinguish "orphan schema (previous failed attempt)" from "real registered project"

### Delta application

`apply_fork_delta_from_files(fork_schema_name, parent_project_id, files, project_root, org_db)`:

1. Get stored hashes for files from fork schema (which mirrors parent HEAD)
2. Parse functions at target commit from client-provided file contents
3. Upsert nodes whose `body_hash` differs
4. Delete nodes from changed files that don't exist at target commit
5. Returns `{updated, deleted, unchanged}`

### Critical bug fixed 2026-07-03: project_id mismatch in forked schemas

`fork_schema()` copies rows from parent → fork via `INSERT INTO fork SELECT * FROM parent`. This preserved the parent's `project_id` (e.g. `'django'`) on every copied row. All MCP tools filter by `project_id = fork_project_id` (e.g. `'django-fork-c84b91b7'`) and returned zero results — the fork appeared empty despite having 26K nodes.

**Fix (committed f9fd65e7):**
1. `storage.py`: new `update_project_id_in_schema()` — bulk UPDATE immediately after `fork_schema` copy
2. `fork.py` `create_fork_from_files`: calls `update_project_id_in_schema()` after `fork_schema()`
3. `fork.py` `apply_fork_delta_from_files`: now accepts `fork_project_id` kwarg and writes delta nodes with the fork's ID

**Repair for existing broken forks (run on TheHive as superuser):**
```sql
UPDATE django_fork_c84b91b.nodes SET project_id = 'django-fork-c84b91b7';
UPDATE django_fork_c84b91b.edges SET project_id = 'django-fork-c84b91b7';
UPDATE django_fork_c84b91b.function_embeddings SET project_id = 'django-fork-c84b91b7';
```

### Performance characteristics

Django fork at commit c84b91b7 (org_benchmark, 2026-07-03):
- `fork_schema` bulk copy from org_demos data: fast (data already in DB)
- `fork-from-files` with 1,944 changed files: 13.5MB payload, ~300s
- Resulting fork: 26,024 nodes confirmed live via `get_project_home`
- Base `django` project in org_benchmark: 11,169 nodes (partial — some index batches failed under load)

---

## 8. MCP Tool Reference

Full reference with parameters, examples, and auth requirements.

### Discovery tools

#### `list_projects`
List all indexed projects with node count, edge count, last-indexed timestamp.

#### `get_project_home(project_id)`
Full architectural snapshot: subsystems, wiring diagram, chokepoints, entry points, risk surface, health, recent decisions. **Call first at every session start.** Returns `risk_detection_mode: "structural_heuristic_no_decisions"` when no decisions are logged yet.

#### `query_similar_functions(snippet, project_id?, top_k?)`
Hybrid BM25 + semantic search (RRF fusion). `top_k` default 10. Returns results with `similarity` (0–1) and `_guidance` field with signals from the Guidance Layer.

#### `get_function_context(function_name, project_id?)`
Unified pipeline: node metadata + callers + callees + impact radius + decision history + similar functions. Use when you have a function name and want everything.

#### `get_callers(function_name, project_id?)`
All functions that call the named function.

#### `get_callees(function_name, project_id?)`
All functions called by the named function. `is_external` flag distinguishes library calls.

#### `get_impact_radius(function_name, depth?, project_id?)`
Recursive BFS of all dependents. `depth=2` default. Returns `impact_depth` annotation. Also returns `co_change_hints` with three signals:
- `protocol_completeness` — e.g., `__eq__` without `__hash__`
- `semantic_sibling` — conceptually similar but call-graph-unreachable functions
- `co_change_history` — functions that co-changed with this one, from two sources merged:
  - **git_history**: functions appearing in the same commit ≥3 times (`commit_function_changes` table, populated via `push_cochange_history.py`)
  - **decision_memory**: functions linked in the same `log_decision` call ≥2 times (`decision_functions` table, zero API cost, populated automatically as decisions are logged)
  - Decision memory is the preferred signal — it's semantic (intentional co-changes) vs. noisy git history (all functions in a changed file)

Use `depth=1` for chokepoints (depth=2 can return 70+ functions for heavily-called nodes).

#### `get_subsystem_detail(project_id, subsystem_name)`
Full function list and wiring for one subsystem. Call before reading any file in that subsystem.

#### `find_dependents(function_name, project_id?)`
Flat list of all transitive dependents.

### Decision memory tools

#### `get_decision_history(function_name, project_id?)`
All logged decisions linked to this function. Returns `[]` if `log_decision` was never called. Always run before editing a function you didn't write.

#### `search_decisions(query_text, project_id?)`
Semantic search over decision memory by topic, not function name.

#### `log_decision(project_id, type, description, linked_function_ids?, rejected_alternatives?, trigger?)`
Record a design decision. Types: `Architectural | Design | Implementation | Patch`. `trigger: "git:<short-hash>"` for commit-linked decisions. **Requires auth** (owner or write access).

### Contract tools

#### `list_contracts(project_id?)`
List active contracts.

#### `create_contract(project_id, title, natural_language, function_ids?)`
Generate contract from plain-English rule using Claude Haiku. **Requires auth.**

#### `approve_contract(contract_id)`
Embed examples and activate. **Requires auth.**

#### `check_contracts(project_id, function_ids?)`
Check functions against all active contracts. Returns violations. Called by post-commit hook.

#### `list_improvements / file_improvement / resolve_improvement`
Agent improvement suggestion workflow.

### Performance tools

#### `check_performance(project_id, exclude_test_files?)`
Detectors: `correlated_join_aggregate`, `n_plus_one`, `quadratic_expansion`, `external_call_in_loop`, `sequential_awaits`. Default excludes test files. Returns `structural_causes` mapping pattern → root cause.

#### `dismiss_performance_concern / dismiss_solid_concern`
Acknowledge a finding as intentional.

#### `check_solid_principles(project_id)`
SRP/OCP/DIP detectors. Undocumented as of Phase 16 — add to Phase 17 docs.

### Dependency fingerprint tools

All require SCIP augmentation (`index_scip` or `index_project` with `scip-python` installed). Currently return empty with guidance note for all demo repos.

- `get_dependency_fingerprint(project_id)` — latest external symbol snapshot
- `list_dependency_fingerprint_history(project_id, limit?)`
- `compare_dependency_fingerprints(fingerprint_id_a, fingerprint_id_b)`
- `list_external_dependencies(project_id)` — all external symbols called
- `get_library_dependents(library_name, project_id)` — internal functions calling a library

### Indexing tools

#### `index_project(path, project_id?)` **[auth required]**
Full index from server-side path. Enqueues background job; returns `job_id`.

#### `index_changes(changed_files, file_contents, project_root?, project_id?)` **[auth required]**
Incremental update — only changed files. Synchronous (fast). Called by post-edit hook.

#### `index_scip(path, project_id?)`
Ingest SCIP index for external dependency data.

#### `reembed_project(project_id)` **[auth required]**
Force re-embedding of all functions after changing embedding model.

#### `enrich_summaries(project_id, limit?, force?)` **[auth required]**
Generate LLM summaries for functions without docstrings. Background job.

### Setup tool

#### `setup_scopenos_client(project_id, project_root, server_url?)`
Generates: post-commit hook, Claude Code settings, CLAUDE.md section, memory files, and `acip-workflow` skill.

**Known issue (Phase 16 #4):** Hardcodes `http://localhost:3004` if `server_url` not passed. Always pass `server_url` explicitly for remote instances.

### Fork tools

#### `fork_project(parent_project_id, target_commit, fork_project_id, repo_path)` **[auth required]**
Server-side fork using local git. Deprecated in favor of `POST /api/fork-from-files`.

#### `drop_fork(fork_project_id)` **[auth required]**
Drop fork schema and deregister from projects table.

### Branch tools (untested — requires branch tracking setup)

`compare_branches(project_id, branch_a, branch_b)`, `get_branch_conflicts(project_id)` — no branch data in current deployment.

### Pre-code validation

#### `validate_proposed_code(project_id, file_path, proposed_code)`
Conformance score (0–1) + deviation list. Checks: naming convention, async convention, sequential awaits, DB-in-loop. See Guidance Layer section for signal details.

#### `preflight_architecture(project_id)`
Coupling hotspots, external scatter, duplication clusters.

---

## 9. Guidance Layer

19 signals injected into 6 MCP tool responses. All fire in <20ms via pure logic or 1–3 parallel DB queries. No LLM calls.

### `query_similar_functions` — 7 signals

| # | Signal | Fires when | Cost |
|---|---|---|---|
| 1 | **Concentration** | ≥75% of results in one module | Zero (pure) |
| 2 | **Chokepoint** | Any result has ≥15 callers | 1 batch query on `edges` |
| 3 | **Decision gap** | High-caller function has no logged decisions | 1 batch query on `decision_functions` |
| 4 | **Contract** | Active contract covers a result | 1 query on `contracts` |
| 5 | **Performance** | Async functions in I/O-heavy module (static allowlist) | Zero |
| 6 | **Async distribution** | ≥80% or ≤20% async in results | Zero |
| 7 | **Naming convention** | ≥60% share a verb prefix | Zero |

### `get_callers` — 3 signals

| # | Signal | Fires when |
|---|---|---|
| 8 | **Caller concentration** | ≥75% in one module OR ≥5 different modules |
| 9 | **Chokepoint warning** | ≥15 callers → suggests `get_impact_radius` |
| 10 | **Async caller context** | All callers async → or <30% async |

### `get_callees` — 3 signals

| # | Signal | Fires when |
|---|---|---|
| 11 | **External dependency surface** | Any callee has `is_external=True` |
| 12 | **Adapter layer suggestion** | ≥3 external callees |
| 13 | **Internal callee concentration** | ≥75% of internal callees in one module |

### `get_decision_history` — 1 signal

| # | Signal | Fires when |
|---|---|---|
| 14 | **Empty decision case** | `decisions == []` → suggests `get_callers` + `log_decision` |

### `check_performance` — 1 signal

| # | Signal | Always fires |
|---|---|---|
| 15 | **Pattern → structural cause** | Maps detector pattern to root cause + fix direction |

### `validate_proposed_code` — 4 conformance checks

| # | Check | Severity | Deduction |
|---|---|---|---|
| 16 | **Naming conformance** | Medium | −0.15 |
| 17 | **Async conformance** | High (sync in async module) / Medium | −0.25 / −0.15 |
| 18 | **Sequential awaits** | High | −0.25 |
| 19 | **DB-in-loop** | High | −0.25 |

Score = `max(0.0, 1.0 - sum(deductions))`.

### Signal coverage gaps

Known gaps not yet implemented:
- Cross-module import boundary checks (import edges exist in DB, not checked)
- Temporal coupling (co-change history from `branch_function_changes`)
- Branch conflict risk (same table, cross-branch query)
- Return type consistency in `validate_proposed_code`
- Decorator consistency check
- Module-relative CHOKEPOINT_THRESHOLD (currently hardcoded at 15)

---

## 10. Contract Enforcement

### How contracts work

1. `create_contract` — Haiku parses natural language → violation examples + structural expression
2. `approve_contract` — Embeds examples, activates
3. `check_contracts` — Runs structural check (`_check_structural`) and semantic check (`_check_semantic`)
4. Post-commit hook calls `check_contracts` automatically

### Contract rule types

- **SEMANTIC** — embedding similarity against violation examples
- **BOUNDARY** — call graph boundary constraints (prohibited callee patterns)
- **PRESENCE** — required callee must be present

### Bypass vectors (identified 2026-06-16)

**Structural enforcement gaps:**
1. Depth-1 call graph only — one wrapper layer defeats the check
2. `required_callee` trivially satisfiable by calling a no-op stub
3. Name matching is last-segment only — rename or move the prohibited function

**Semantic enforcement gaps:**
4. Semantic input is signature+docstring, not body — put violation in implementation
5. `check_project` is structural-only — semantic path only fires in `check_functions`

**Enforcement trigger gaps:**
6. No write-time gate — `index_changes` doesn't check contracts
7. Agent doesn't see contracts before editing — `get_project_home` shows count but not rules
8. Hook is optional per repo

**Scoping gaps:**
9. `function_ids` exact-match only — new functions added to a pattern aren't auto-covered
10. `scope_exclusions` is broad and prefix-based with no audit trail

---

## 11. Session Identity System

Five layers of protection for AI agents operating on live infrastructure. See `docs/session-isolation-eli5.md` for the full ELI5 explanation.

### The six session types

| Session | Postgres role | What it can do |
|---|---|---|
| **reader** | `scopenos_read` | SELECT only — no writes |
| **indexer** | `scopenos_control_rw` | Read + write index data. No ALTER/DROP |
| **deployer** | (no DB at all) | K8s restart/check only |
| **migrator** | `scopenos_migrator` | CREATE SCHEMA. No DROP DATABASE |
| **tester** | `scopenos_test_runner` | Full access to test DB. Zero access to prod |
| **provisioner** | `scopenos_provisioner` | CREATE DATABASE. No touch existing data |

### Five protection layers

1. **Docker containers (rooms)** — Each session in its own container, torn down on session end
2. **Postgres roles (ID badges)** — Hard backstop at the DB server. Even if a session knew another's password, the role's permissions don't change
3. **Separate secrets directories (key cabinets)** — `/run/secrets/` only contains that session's password; other sessions' files are never mounted
4. **Wrapper script (keycard reader)** — Reads password → writes to `~/.pgpass` → constructs `DATABASE_URL` without password → **deletes password from environment** → launches Claude
5. **Isolated conversation transcripts** — `~/.claude/sessions/<type>/` separate per session type

### Role capabilities

```
scopenos_read         → SELECT only
scopenos_control_rw   → Read + write index data. No ALTER or DROP
scopenos_migrator     → Can CREATE SCHEMA. Cannot DROP DATABASE
scopenos_provisioner  → Can CREATE DATABASE. Cannot touch existing data
scopenos_test_runner  → Full access to test DB. Cannot reach production
```

### File map

```
~/.config/agent-of-empires/profiles/
  scopenos-reader/config.toml
  scopenos-indexer/config.toml
  scopenos-deployer/config.toml
  scopenos-migrator/config.toml
  scopenos-tester/config.toml
  scopenos-provisioner/config.toml

~/scopenos-sessions/
  claude-reader   claude-indexer   claude-deployer
  claude-migrator claude-tester    claude-provisioner

~/.secrets/
  reader/db_password   indexer/db_password   migrator/db_password
  tester/db_password   provisioner/db_password

~/.kube/scopenos/
  scopenos-reader.yaml    (read-only cluster view)
  scopenos-deployer.yaml  (patch deployments only)
```

---

## 12. Kubernetes & Operations

### K8s pod has no Python

The Scopenos K8s pod does NOT have a Python binary — confirmed 2026-07-02. `kubectl exec` into the pod will fail with "executable not found". Use the psql DO block approach (via `docker exec -it acip-postgres psql`) or the sandbox container for any scripting.

### Pod components

```yaml
# k8s/namespace.yaml — namespace: scopenos
# k8s/api-deployment.yaml — scopenos-api deployment
# k8s/worker-deployment.yaml — scopenos-worker (RQ)
# k8s/redis.yaml — Redis in-cluster (stateless)
# k8s/ingress.yaml — Traefik ingress "/" → scopenos-api-svc:3004
# k8s/hpa-api.yaml — HPA for API pods
# DO NOT apply k8s/postgres.yaml — Postgres runs outside cluster
```

### Useful commands

```bash
# Pod status
kubectl get pods -n scopenos

# API logs (stream)
kubectl logs -l app=scopenos-api -n scopenos -f

# Worker logs
kubectl logs -l app=scopenos-worker -n scopenos -f

# Queue depth
kubectl exec -it deployment/redis -n scopenos -- redis-cli llen rq:queue:scopenos-indexing

# Force rollout after image push
kubectl rollout restart deployment/scopenos-api deployment/scopenos-worker -n scopenos

# Scale workers
kubectl scale deployment scopenos-worker --replicas=3 -n scopenos

# Backup Postgres
docker exec scopenos-postgres pg_dump -U scopenos --no-owner scopenos > scopenos-backup.sql

# Restore
docker exec -i scopenos-postgres psql -U scopenos scopenos < scopenos-backup.sql
```

### CI/CD

Push to `main` → GitHub Actions → build Docker image → push to registry → `kubectl apply` + rollout restart. Tailscale OAuth grants CI access to the home cluster.

### Deploying from this container

```bash
# Check CI status
gh run list --limit 5

# Trigger manual deploy (if needed)
kubectl rollout restart deployment/scopenos-api -n scopenos
```

### Active state (2026-07-04)

Only org: **`scopenos`** (Cole's org). User: `cole.flicek@gmail.com`.

Active org-scoped keys: `env-primary`, `cole-scopenos-primary` (raw values lost), `claude-code-2026-07` ← current live key in `.mcp.json`.

Active admin keys (no org, dashboard only): `cole-admin` ×3.

**Active non-admin org keys (as of 2026-07-04):**

| Key name | User | Org | Routes to |
|---|---|---|---|
| `demos-indexer` | `demos@scopenos.internal` (id: `973f8c7f-...`) | `demos` | `org_demos` |
| `demos-indexer` | `cole.flicek@gmail.com` | `demos` | `org_demos` |
| `benchmark-indexer` | `benchmark@scopenos.internal` | `benchmark` | `org_benchmark` |

- `demos-indexer` (demos@): key `scopenos-0f0f290e5ad8e62fed664cf05455038c15`. Use for `POST /api/enrich-summaries/{project_id}` on demo projects.
- `benchmark-indexer`: value = `BENCH_API_KEY` in `.mcp.json`. Routes to `demos` org → `org_demos` DB (migrated 2026-07-04 via `UPDATE api_keys SET org_id = 'demos' WHERE name = 'benchmark-indexer'`). Forks created via this key land in org_demos and inherit all enriched summaries via SQL copy at zero cost. `org_benchmark` DB is now unused.

**Lessons from demos key provisioning (2026-07-04):**
- The DO block for key creation must SELECT the user AFTER inserting, or the user_id stored in api_keys may come from a prior incomplete insert (mismatched UUIDs).
- If a key was previously created and revoked (`revoked_at` is set), `get_user_by_key` returns None → 401. Always check `revoked_at` when debugging 401s on known-good keys.
- DO blocks run with the `scopenos` superuser's default search_path (`"$user", public` = `scopenos, public`) — inserts go to `scopenos.*` tables, matching the server's search_path.
- `sync=true` for enrichment must be passed in the **JSON body**, not as a query parameter. The endpoint reads it via `body.get("sync", False)`.

```bash
# Check what orgs exist
docker exec -it acip-postgres psql -U scopenos scopenos \
  -c "SELECT slug FROM organizations;"

# Check active API keys (no passwords)
docker exec -it acip-postgres psql -U scopenos scopenos \
  -c "SELECT k.name, u.email, k.org_id, k.created_at FROM api_keys k JOIN users u ON u.id = k.user_id WHERE k.revoked_at IS NULL ORDER BY k.created_at DESC LIMIT 10;"
```

### Re-issuing an API key (when raw key is lost)

Pure psql — no Python required. Run from TheHive. The key prints in the NOTICE line.

```bash
docker exec -it acip-postgres psql -U scopenos scopenos
```

Paste this DO block:

```sql
DO $$
DECLARE
  v_raw  text;
  v_hash text;
  v_uid  uuid;
BEGIN
  v_raw := 'scopenos-' || left(
    md5(random()::text || clock_timestamp()::text) ||
    md5(clock_timestamp()::text || random()::text),
    34
  );
  v_hash := encode(sha256(convert_to(v_raw, 'UTF8')), 'hex');
  SELECT id INTO v_uid FROM users WHERE email = 'cole.flicek@gmail.com' LIMIT 1;
  IF v_uid IS NULL THEN RAISE EXCEPTION 'User not found'; END IF;
  INSERT INTO api_keys (id, user_id, key_hash, name, created_at, org_id)
  VALUES (gen_random_uuid(), v_uid, v_hash, 'claude-code-' || to_char(NOW(), 'YYYY-MM'), NOW(), 'scopenos');
  RAISE NOTICE 'NEW_KEY=%', v_raw;
END $$;
```

Copy the key from the `NOTICE: NEW_KEY=...` line. Update `/workspace/ACIP/.mcp.json` and restart Claude Code.

Uses only PG 11+ built-ins (`sha256`) and PG 13+ built-ins (`gen_random_uuid`) — no pgcrypto extension required.

### Extracting a K8s secret value

```bash
# From TheHive (outputs full DSN including password — don't paste into chat)
kubectl get secret scopenos-secrets -n scopenos \
  -o jsonpath='{.data.CONTROL_DB_URL}' | base64 -d

# List all keys in the secret
kubectl get secret scopenos-secrets -n scopenos \
  -o jsonpath='{.data}' | tr ',' '\n'
```

### Sandbox credential sources

| Credential | Location in sandbox |
|---|---|
| `scopenos_test_runner` password | `/root/.pgpass` |
| `TEST_DATABASE_URL` | Set from `/root/.pgpass` before running pytest |
| `SCOPENOS_API_KEY` | Shell env for MCP tool calls |
| Other DB passwords | Not available in sandbox — retrieve from TheHive |

### Connecting to databases

**From sandbox:**
```bash
# Test DB (credentials in /root/.pgpass)
TEST_DATABASE_URL="postgresql://scopenos_test_runner:<pw>@172.21.0.1/scopenos_test"
uv run python scripts/some_script.py

# Control DB or org DB — get CONTROL_DB_URL from TheHive first
CONTROL_DB_URL="<from TheHive>" uv run python scripts/some_script.py
```

**From TheHive:**
```bash
# Superuser (scopenos, not postgres)
docker exec -it acip-postgres psql -U scopenos scopenos

# Via K8s secret
CONTROL_DB_URL=$(kubectl get secret scopenos-secrets -n scopenos \
  -o jsonpath='{.data.CONTROL_DB_URL}' | base64 -d)
```

### Known gaps

| Gap | How to resolve |
|---|---|
| Port mapping for `acip-postgres` on TheHive host | `docker inspect acip-postgres \| grep HostPort` |
| Full list of keys in `scopenos-secrets` | `kubectl get secret scopenos-secrets -n scopenos -o jsonpath='{.data}' \| tr ',' '\n'` |
| Which org databases have been provisioned | `SELECT slug FROM organizations` via `CONTROL_DB_URL` |
| Whether `demos` DB has been created | `docker exec -it acip-postgres psql -U scopenos scopenos -c '\l'` |

### Post-commit hook

Installed at `.git/hooks/post-commit`. Fires on every commit:
1. Sends changed file contents to `/api/index-changes`
2. Calls `/api/decisions` with the commit hash and diff

```bash
# Install
cp /workspace/ACIP/scripts/post-commit.sh .git/hooks/post-commit
chmod +x .git/hooks/post-commit
export SCOPENOS_URL=http://100.71.88.106:3004
```

### Backfilling decision history

```bash
SCOPENOS_URL=http://100.71.88.106:3004 python3 /workspace/ACIP/scripts/backfill_decisions.py
```

One-time per project. Run after initial index. Requires server to be reachable. The `--use-api` flag (default) calls `/api/functions` for each commit to get actual function IDs.

### CLI scripts

| Script | Status | Notes |
|---|---|---|
| `scripts/create_user.py` | Works on pod (venv active) | `--help` fails locally — asyncpg not in system Python |
| `scripts/backfill_decisions.py` | Works — `--dry-run`, `--since`, `--limit`, `--project` flags | |
| `scripts/index_demo_repos.py` | Works — `--repos`, `--skip-enrich`, `--mark-only` flags | |
| `scripts/provision_org.py` | Fixed 2026-07-03 | Now includes `ALTER DEFAULT PRIVILEGES FOR ROLE scopenos_provisioner` so future provisioner-created schemas are accessible to the org role |
| `scripts/provision_benchmark.py` | Needs header fix | Still says "run from container at 172.21.0.1" |
| `scripts/repair_bench_org_grants.sql` | One-time repair | Run against org_benchmark on TheHive to fix pre-seeded schema grants + transfer table ownership after pg_dump copy |

---

## 13. Demo Repos

12 repos indexed as of 2026-06-19. All verified: write tools return 403 for non-admin users (`is_demo → return operation == "read"` in storage.py). Semantic search verified working at 0.70–0.83 similarity.

| Repo | Nodes | Key chokepoints | Notes |
|---|---:|---|---|
| `psf/requests` | 1,097 | `Session` (58 callers), `Request` (64) | Small, focused HTTP client |
| `pallets/flask` | 1,608 | `Scaffold.route` (163 callers) | `setupmethod`, `ProxyMixin` are knowledge gaps |
| `pytest-dev/pytest` | 8,053 | `LineMatcher.fnmatch_lines` (1,750), `Metafunc.parametrize` (312) | Testing framework |
| `mwaskom/seaborn` | 2,923 | `Plot.add` (154), `color_palette` (120) | Two APIs: classic + objects |
| `sphinx-doc/sphinx` | 8,859 | `BuildEnvironment` | 9 subsystems, `sphinx.util` is universal dependency |
| `pydata/xarray` | 8,907 | `DataArray` (1,181), `Dataset` (1,075) | N-dim labeled arrays, NetCDF/Zarr/HDF5 IO |
| `pylint-dev/pylint` | 9,611 | `safe_infer` (137) | AST type inference utility |
| `scikit-learn/scikit-learn` | 12,596 | `raises` (1,220), `assert_allclose` (949) | ML library; `fit`/`predict`/`score` seam |
| `matplotlib/matplotlib` | 11,955 | `add_subplot` (366), `BboxBase` (233), `Path` (274) | 10,053 of 11,955 fns in `lib.matplotlib` |
| `astropy/astropy` | 19,420 | `TableColumns.isinstance` (1,873), `Time` (477), `WCS` (348) | 23 subsystems |
| `django/django` | 42,988 | `QuerySet.annotate` (1,035), `BlockNode.super` (1,779) | Largest demo repo; `get_project_home` → 141KB |
| `sympy/sympy` | 38,747 | `Symbol` (1,717), `Rational` (1,209) | `sympy.polys` (6,446 fns) is largest subsystem |

### Django DB health snapshot (2026-06-21)

| Metric | Count | Coverage |
|---|---:|---:|
| Nodes | 42,988 | — |
| With embedding | 42,988 | 100% |
| With LLM summary | 12,075 | 28.1% |
| With docstring | 7,575 | 17.6% |
| Orphan nodes | 6,972 | 16.2% |

Enrichment backlog: 21,987 nodes without summary, estimated $6.60 to enrich.

**Benchmark readiness: 🟡 Partial** — embeddings 100% but summary coverage at 28% degrades `query_similar_functions` result quality.

---

## 14. Known Limitations

### Analysis limitations

**Decision memory is empty for most functions**
`get_decision_history` returns `[]` for virtually every function. `backfill_decisions.py` has never been run against production indexes. `risk_detection_mode: "structural_heuristic_no_decisions"` in `get_project_home` is the indicator. Impact: risk scoring falls back to structural heuristics only.

**SCIP data is empty for all indexed projects**
`list_external_dependencies`, `get_library_dependents`, `get_dependency_fingerprint` all return empty. `scip-python index` + `index_scip` has never been run on any demo repo. Fix: run `scip-python index` + `index_scip` per repo (2–4 min each).

**`app.src.*` namespace duplication in Scopenos's own index**
Functions appear under both `src.module.function` and `app.src.module.function` (Docker build artifact). `get_callers` may return half the actual callers for Scopenos functions. Don't expose Scopenos's own index as a demo repo.

**Branch tools untested**
`compare_branches` and `get_branch_conflicts` have no branch data. Tools compile and schema supports it, but end-to-end unverified.

**`estimate_index` requires server-side path**
Remote clients can't use it. Fix: accept file contents inline (like `index_changes`).

### Scale ceilings

| Constraint | Current limit | Notes |
|---|---|---|
| asyncpg connection pool | max=10 | Raise to 50 before high-concurrency load |
| HNSW index | Not applied | Apply before >100K embeddings for query performance |
| enrich_summaries per job | 2,000 functions cap | Prevents runaway LLM costs |
| RQ job queue | 1 concurrent job per user | Redis rate key per user_id |
| Django `get_project_home` | 141KB response | Hits inline MCP result limit — use jq extraction |

### False positive patterns by detector

**n_plus_one:** Small fixed-size collection loops; test setup functions inserting rows in a loop.

**external_call_in_loop:** Batch API clients designed to be called in a loop; loops inside generator expressions.

**sequential_awaits:** Intentionally ordered sequential awaits (acquire lock, then use resource).

**quadratic_expansion:** Scientific computing code with intentional O(n²) operations (numpy outer products, pairwise distance).

### Unsupported language patterns

- Decorators that change function signatures — decorator tracked as call edge but parameter names reflect wrapped function
- `__getattr__`-based dynamic dispatch — invisible to call graph
- Metaclass-generated methods — not indexed
- TypeScript anonymous functions — auto-generated IDs may not match between index runs
- Generic fallback parsers (Zig, Groovy, Perl) — body captured but no call edges

### Benchmark infrastructure gaps (as of 2026-07-03)

**MCP routing confirmed working:** `scopenos_bench` server entry in `.mcp.json` routes `BENCH_API_KEY` to org_benchmark. Subagents call `mcp__scopenos_bench__*` tools and get real call graph data from forks (confirmed: `get_project_home` returned 26K nodes, `query_similar_functions` found `TimezoneMixin.get_tzname` for the timezone bug).

**Fork project_id bug fixed:** All forks created before 2026-07-03 have `project_id = parent_id` on copied rows. Repair: `UPDATE {fork_schema}.nodes SET project_id = '{fork_id}'` on TheHive. Future forks use `update_project_id_in_schema()` automatically.

**Bench venv must use Python ≤ 3.12:** Django 2.2-era source uses `import cgi` (removed in Python 3.13). Bench venv created with `uv venv` defaults to system Python 3.14 — use `uv venv --python python3.12` or set `BENCH_PYTHON=/path/to/python3.12` before running `benchmark.run setup`.

**Bench base index batching causes server overload:** Sending 914 files in 50-file batches (18 HTTP requests) overwhelms the single pod and causes 502/reset errors. Alternative: copy base project schemas from org_demos via `pg_dump | pg_restore` on TheHive (done for current benchmark org).

**Path B result contamination:** benchmark/results/ directory is in the repo. Agents CAN read previous patches if not explicitly forbidden. Exclude `benchmark/results/` from agent context or move results outside the worktree for clean comparisons.

### What a paying user might hit first

1. `get_decision_history` returns nothing — see decision memory section
2. `query_similar_functions` returns unrelated results — no docstrings, enrichment not run
3. `check_performance` flags test helpers — default now excludes test files, but may still fire for tests outside `tests/`
4. `list_external_dependencies` returns empty — SCIP not run
5. `get_project_home` returns 141KB for django — hits MCP inline result limit

---

## 15. Phase 16 Audit

Conducted 2026-06-19. Auditor: Claude Sonnet 4.6 against production server.

### Critical blockers (block Phase 17)

**1. All 14 HTTP write endpoints have no auth**
MCP tools correctly call `check_permission()`. HTTP endpoints used by the web UI do not. Anyone reaching port 3004 can delete any project's entire index, push arbitrary data, trigger expensive LLM calls, etc.

Risk endpoints: `DELETE /api/projects/{id}` (highest risk), `POST /api/index-bulk`, `POST /api/enrich-summaries/{id}`, `POST /api/reembed/{id}`, all contract mutation endpoints.

Intentionally unauth'd (git hook paths): `POST /api/decisions`, `POST /index` — document and network-restrict.

**2. LSP tools fail with local paths**
`lsp_get_diagnostics`, `lsp_get_definition`, `lsp_find_references` all run on the server filesystem. Remote clients pass local paths → generic `[Errno 2] No such file or directory` error.

### Medium findings (fix before Phase 17)

**3.** `check_performance` too noisy — 57 findings on Scopenos itself including test code
**4.** `setup_scopenos_client` hardcodes `http://localhost:3004` regardless of actual server URL
**5.** `list_external_dependencies` / `get_library_dependents` return empty without explaining why
**6.** `get_dependency_fingerprint` returns 0 libraries without explaining SCIP requirement

### Low findings (document, don't block)

**7.** `check_solid_principles` / `dismiss_solid_concern` exist and work but undocumented
**8.** Branch tools — add to docs as "requires branch tracking setup"
**9.** `POST /api/decisions` / `POST /index` — document as intentionally public

### MCP tool verdicts

**✅ Ship (40+ tools):** All read/query tools, all auth-protected MCP write tools, `validate_proposed_code`, `preflight_architecture`, all hooks.

**🔧 Fix before Phase 17:** LSP tools (3), dependency tools without guidance notes (3).

**⚠️ Untested:** `compare_branches`, `get_branch_conflicts`, `get_function_at_commit`, `estimate_index`.

### HTTP endpoint verdicts

**✅ Ship (read-only):** All GET endpoints, `POST /api/search`, `POST /api/functions`, `POST /api/contracts/check`.

**🔧 Fix before Phase 17 (missing auth):** All 14 write endpoints listed in Critical Finding #1.

---

## 16. Architecture Deepening Decisions

Five major refactoring decisions from the architecture improvement session. Full reasoning in `docs/architecture-deepening.md`.

### Change 1 — Extract `ArchitectureAnalyzer` from `CallGraphDB`

**Problem:** `get_project_home_data()` (161 lines of analytics) lived inside a class named after database operations. HTTP framework patterns hardcoded at `storage.py:1025`.

**Deletion test:** Remove the method — DB class stays complete. Complexity reappears in an analysis class where it belongs.

**What changed:** `src/call_graph/models.py` (new) → `GraphData`, `ArchitectureSnapshot` typed dataclasses. `src/analysis.py` (new) → `ArchitectureAnalyzer(http_patterns=DEFAULT_HTTP_PATTERNS)` — pure sync class, takes `GraphData`, returns `ArchitectureSnapshot`. `get_project_home_data()` reduced to 5 lines: fetch → analyze → save → return.

**Key decision:** Option A (pure data bundle) over Option B (analyzer holds DB reference) — Option A makes the seam explicit: storage fetches raw data, analyzer transforms it, wrapper commits side effect. Each piece is testable in isolation.

### Change 2 — Extract `EmbeddingPipeline` from `EmbeddingStore`

**Problem A:** `EmbeddingStore.upsert_chunks` was writing to `nodes` table via `self._db._db` — an ownership violation.
**Problem B:** Two-tier routing strategy (documented → small model; undocumented → large model) buried in a storage method.

**What changed:** `src/embeddings/pipeline.py` (new) → `EmbeddingPipeline(db, store)` owns routing and orchestration. `EmbeddingStore` retains only vec0 table operations. Node metadata writes go through `CallGraphDB.update_node_embedding_meta()`.

**Key insight:** The `self._db._db` accesses that remain in `EmbeddingStore` are legitimate — it owns the vec0 tables. The breach that mattered was writing to `nodes`, which is CallGraphDB's domain.

### Change 3 — `ContractRule` extraction from `ContractManager`

**Problem A:** `_check_structural` used `self._db._db.execute(...)` for edge queries that already exist as `CallGraphDB.get_callees()`.
**Problem B:** Rule matching logic (prohibited callee, required callee) was inline in async methods — untestable without a database.

**What changed:** `src/contracts/rule.py` (new) → `ContractRule` dataclass. `from_expr(expr)` classmethod parses JSON expression. `find_prohibited_callees(callee_ids)` and `is_excluded(function_id)` are pure methods taking plain lists. No I/O.

**Why not a class hierarchy:** Three rule types but same structural check logic. A single dataclass with predicate methods handles all three. Don't add abstraction for hypothetical futures.

### Change 4 — `Services` typed dataclass in `server.py`

**Problem:** `_services: dict[str, Any]` — typos raise `KeyError` at runtime, hidden dependencies, dict falsy check bug.

**What changed:** `Services` dataclass with typed fields. `_services: Services | None = None`. All `svcs["key"]` → `svcs.key`. Added `global _services` (needed because we now assign, not mutate in place).

**Why not full DI:** FastMCP doesn't support FastAPI-style `Depends()` injection. Service locator pattern remains, but accessing the wrong key is now a type error, not a runtime `KeyError`.

### Change 5 — `EmbeddingPipeline.model` public property

**Problem:** `indexer.py` accessed `self._pipeline._model` — crossing a module boundary to read a private property.

**What changed:** `_model` → `model` (public property). The configured embedding model name is part of the pipeline's interface — callers have a legitimate reason to know it for log messages and reporting.

### What was deliberately NOT changed

**Indexer non-atomic orchestration:** Call graph commits before embedding writes. On embedding failure, call graph has new nodes but vectors are stale. Self-healing: hash-diff mechanism detects stale embeddings on next run. The risk/reward of the full Parse/Reconcile/Commit decomposition doesn't justify touching the Indexer's main path without dedicated test coverage.

**`web/routes.py` `db._db.execute()` breach:** `api_status` uses raw SQL to count nodes/edges. Same pattern fixed in `EmbeddingStore` but not here — requires adding `count_nodes()`/`count_edges()` methods to `CallGraphDB`. Noted for a future session.

---

## 17. Business Case & Commercialization

### Pricing model

Project-based tiers, not per-seat.

| Tier | Price | Limits | Target |
|---|---|---|---|
| **Free** | $0 | 1 project, 5K functions, 30-day history | Solo evaluation |
| **Pro** | $29/month | 5 projects, 50K functions, unlimited history | Freelancers, AI-heavy solo devs |
| **Team** | $99/month | 20 projects, 200K functions, Slack support | Startups |
| **Business** | $299/month | Unlimited projects, 500K functions, SLA, SSO | Mid-market |
| **Enterprise** | $2K–8K/month | On-prem option, custom LLM, audit logs | Fortune 500 |

### Cost structure

**Embedding:** OpenAI `text-embedding-3-small` at $0.02/1M tokens (~$0.004 per 1K functions).
**Summarization:** Claude Haiku at ~$0.30/1K functions.
**Ongoing:** ~10% of initial index cost per month (hash-diffing means only changed functions re-embed).

| Customer Size | Functions | Initial Cost | Monthly |
|---|---|---|---|
| Small startup | 5K | ~$1.80 | ~$0.18 |
| Mid-size | 50K | ~$18 | ~$1.80 |
| Large company | 200K | ~$72 | ~$7.20 |
| Enterprise | 1M+ | ~$360 | ~$36 |

**Infrastructure:** $200–400/month at 500 customers. Under $1/customer/month. Gross margins: 83–95%.

### Revenue scenarios

| Scenario | Mix | MRR | ARR |
|---|---|---|---|
| Early traction | 50 Pro, 20 Team | $3,430 | $41K |
| Anthropic listing | 200 Pro, 80 Team, 20 Business | $15,780 | $189K |
| First enterprise deal | Above + 1 MEGA | $23,780 | $285K |
| Real GTM motion | 500 Pro, 200 Team, 50 Business, 5 MEGA | $66,500 | $798K |

**Path to $1M ARR:** ~600–700 paying customers, achievable in 18–24 months with proper distribution.

### Defensibility

**Existential risk:** Anthropic ships a native version inside Claude Code.

**Why not fatal:**
1. Decision memory is proprietary data — 2 years of history doesn't migrate. Switching cost grows with use.
2. On-prem is a structural moat — Anthropic won't offer this for security-conscious enterprises.
3. MCP ecosystem is open — Invariant Contracts, multi-agent coordination, decision memory substrate are differentiated enough to survive alongside a native Claude Code feature.

### Go-to-market

**Primary:** Claude Code ecosystem — MCP server directory, Claude Code community (Discord/Reddit/X), GitHub repo.

**Secondary:** Developer content — benchmark comparison (ACIP vs. no ACIP on real tasks) is the clearest marketing asset.

**Enterprise:** Bottom-up motion — developer → team → manager.

### What needs to be built for SaaS

| Component | Effort | Notes |
|---|---|---|
| Authentication | Medium | Phase 10 complete — API keys working |
| Billing | Medium | Stripe; per-project metering |
| Usage limits | Low | Rate limiting + function count caps at MCP layer |
| Admin dashboard | Medium | Customer health, usage, billing status |
| Multi-tenant hardening | Low | Architecturally isolated; needs audit |
| Data export | Low | GDPR; dump org DB per customer |
| SSO/SAML | High | WorkOS; needed for Business+ tier |
| Audit logging | Medium | Append-only + webhook stream |
| Azure/Bedrock LLM | Medium | Config abstraction already exists |
| On-prem packaging | Low | Docker Compose exists; needs installer |

### Step 1: proving it works

Before commercialization, need a measurable benchmark: ACIP vs. no-ACIP on the same complex coding tasks against the same codebase. The SWE-bench fork infrastructure enables this. Running and publishing results is the single most valuable thing to do right now.

---

## 18. Roadmap

### Completed phases

| Phase | What | Status |
|---|---|---|
| Phase 10 | Auth — users, API keys, project permissions | ✅ Complete |
| Phase 11 | Postgres + pgvector (replaced SQLite + sqlite-vec) | ✅ Complete |
| Phase 12 | Background worker queue (Redis + RQ) | ✅ Complete |
| Phase 13 | Kubernetes deployment (K3d on TheHive) | ✅ Complete |
| Phase 14 | Demo repos (12 SWE-bench repos pre-indexed) | ✅ Complete |
| Phase 15 | SWE-bench benchmark infrastructure (fork delta, loader) | ✅ Infrastructure done |
| Phase 16 | Public website + signup funnel audit | ✅ Audited; blockers identified |

**Multi-tenancy migration (schema-per-project):**
- Org provisioning flow (CREATE DATABASE + apply schema + create org role)
- Schema-per-project isolation with `derive_schema_name`
- OrgRouter with per-org pool cache
- Fork infrastructure (`fork_schema`, `create_fork_from_files`, `apply_fork_delta_from_files`)
- Session identity (six session types, five protection layers)

### Open for Phase 17

**Critical blockers (from Phase 16 audit):**
- Add `X-API-Key` check to all 14 HTTP write endpoints
- Fix LSP tool path resolution for remote clients
- Reduce `check_performance` noise (pre-dismiss known false positives)
- Fix `setup_scopenos_client` localhost URL hardcoding
- Add `_guidance` note to `list_external_dependencies` / `get_library_dependents`
- Add `_guidance` note to `get_dependency_fingerprint`

**Active pending tasks:**
- End-to-end multi-org integration test using Cole's own projects (Task #24)
- Test migration against production DB clone before live data (Task #18)
- Update scopenos-test-guard plugin for TheHive production (Task #14)
- Fix `provision_benchmark.py` header (still says "run from container at 172.21.0.1")
- Fix venv creation for actual test runs (`python3.14-venv` not installed, need `uv venv` fallback)

**GitHub Issues pending:** #14, #15, #18, #24, #29 (node_count fix)

### Feature backlog

**High priority:**
- Invariant Contracts — fix bypass vectors (especially depth-1 call graph and write-time gate)
- PM Interface — surface contract rules in `get_project_home` health section before edits
- Risk-Gated Deploy — block deployment on contract violations detected by `index_changes`

**Medium priority:**
- Temporal Semantic Graph — track when function embeddings drift across commits
- Multi-Agent Coordination — shared decision memory with concurrent agent awareness
- Observability Loop — `log_decision` called automatically on session end
- SCIP augmentation — run `scip-python` on all demo repos for external dependency data
- HNSW index — apply before >100K embeddings per project
- Connection pool size — raise from 10 to 50 before high-concurrency load
- Backfill decisions — run `backfill_decisions.py` against all indexed projects
- Guidance Layer additions — return type consistency, temporal coupling signal, decorator consistency

**Optimization opportunities (from WHITEPAPER.md):**
- Parallel LLM summarization — `asyncio.gather` would reduce O(n) serial to O(1) parallel
- KNN over-fetch adaptive strategy — compute per-project fraction at query time
- Edge resolution disambiguation — prefer same-module/package callee on multi-match
- sqlite-vec dimension mismatch recovery — drop only vec tables, not entire DB
- Decision linkage backfill quality — use `/api/functions` for actual function IDs

---

## Appendix: Security Constraints (Never Violate)

These are hard rules established in the codebase and session history:

- Never commit `.mcp.json` with live API keys
- Never commit `src/tools/admin_export.py`
- `TEST_DATABASE_URL` must be set for tests to run (never bypassed)
- Never `git push --force` to main
- Never skip hooks (`--no-verify`)
- Provisioner password lives in `/root/.pgpass` — do not echo or log it
- Do not run `kubectl get secrets` — prints DB passwords into conversation transcript
- Do not run `kubectl exec` — pod env vars contain credentials
- Do not write credentials to files in `/workspace/ACIP` — shared volume
- Do not paste `CONTROL_DB_URL` or any DSN into chat — use directly in terminal only
- `BENCH_API_KEY` must not be logged or committed
- `POST /api/decisions` and `POST /index` are intentionally unauthenticated — document and network-restrict before public launch
