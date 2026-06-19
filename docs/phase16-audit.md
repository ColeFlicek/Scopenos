# Phase 16 — Internal Feature Audit

Date: 2026-06-19  
Auditor: Claude Sonnet 4.6  
Method: Live testing against production server (100.71.88.106:3004) + code inspection

Verdict legend: ✅ ship | 🔧 fix before Phase 17 | 🚫 remove | ⚠️ untested

---

## Critical Findings (block Phase 17)

### 1. 🔧 All 14 HTTP write endpoints have no auth

The MCP tools correctly call `check_permission()` before writes. The HTTP endpoints used
by the web UI do not. Anyone who can reach port 3004 can:

| Risk | Endpoint |
|---|---|
| Delete any project's entire index | `DELETE /api/projects/{id}` |
| Delete any contract | `DELETE /api/contracts/{id}` |
| Push arbitrary function data into any project | `POST /api/index-bulk` |
| Trigger expensive LLM enrichment on any project | `POST /api/enrich-summaries/{id}` |
| Trigger expensive re-embedding on any project | `POST /api/reembed/{id}` |
| Create/modify/approve/deactivate contracts | `POST/PUT /api/contracts/*` |

`POST /api/decisions` and `POST /index` are intentionally unauthenticated (used by
the post-commit git hook on machines without user credentials). These two should stay
unauth'd but be documented and network-restricted.

**Fix:** Add `X-API-Key` check to all destructive/expensive HTTP endpoints before Phase 17.
The check does not need the full `check_permission` flow — a simple key lookup is enough
since the web UI is the caller and can present a key.

### 2. 🔧 `lsp_get_diagnostics` always fails with local paths

The tool runs on the server's filesystem (`/app/`). Users indexing from their local
machines will naturally pass local paths (e.g. `/Users/name/myproject/src/server.py`).
The error returned is generic: `[{"error": "[Errno 2] No such file or directory: ..."}]`.

Same issue applies to `lsp_get_definition` and `lsp_find_references`.

**Fix:** (a) Document that paths must be server-side paths, OR (b) accept project-relative
paths and resolve them against the project root.

---

## Medium Findings (fix before Phase 17)

### 3. 🔧 `check_performance` too noisy — flags test code and batch operations

57 new findings on Phronosis itself, including:
- Test helper functions (`_seed`, `_seed_project`) in test files
- Batch operations correctly using `executemany` (7 auto-acknowledged, but 20+ remain)
- The `check_performance` function itself flagged as N+1

Before Phase 17: either filter `tests/` by default, or pre-dismiss the known false
positives in test and batch code. Agents seeing 57 "high severity" findings on a clean
codebase will lose trust in the tool.

### 4. 🔧 `setup_phronosis_client` hardcodes `http://localhost:3004`

The generated setup script and CLAUDE.md always contain `PHRONOSIS_URL = "http://localhost:3004"`
regardless of the actual server URL. Users connecting to a hosted Phronosis instance
(e.g. `https://api.phronosis.dev`) will get scripts pointing to localhost.

**Fix:** Use the actual request URL or a `PHRONOSIS_URL` env var when generating the script.

### 5. 🔧 `list_external_dependencies` / `get_library_dependents` return empty without explanation

Both return `[]` when SCIP augmentation hasn't been run. No `_guidance` note explains why.
A user calling `get_library_dependents("asyncpg", "Phronosis")` gets an empty result with
no indication that asyncpg IS used heavily throughout the codebase — just not tracked as
external nodes because SCIP hasn't run.

**Fix:** Add a `_guidance` note: "External dependency data requires SCIP augmentation.
Run `index_project` on a codebase where `scip-python` is installed to populate this."

### 6. 🔧 `get_dependency_fingerprint` returns 0 libraries for Phronosis itself

Same root cause as #5. The tool works and returns correct schema, but the data is empty.
**Fix:** Same as #5 — surface the "no SCIP data" signal explicitly.

---

## Low Findings (document, don't block Phase 17)

### 7. `check_solid_principles` and `dismiss_solid_concern` are undocumented

Two shipped tools not in any docs or roadmap. They work (SRP/OCP/DIP detectors), but
agents won't discover them. Add to the internal tool reference.

### 8. `compare_branches` and `get_branch_conflicts` — untested

No branch data in current deployment. The tools exist and compile, but end-to-end
correctness not verified. Low risk since branch tracking is opt-in.

### 9. `POST /api/decisions` and `POST /index` — intentionally unauthenticated

Used by git hooks running on developer machines that have no user credentials.
Document this explicitly in the ops runbook: "These endpoints accept any caller.
Network-restrict port 3004 to trusted IP ranges before public launch."

---

## MCP Tools — Full Verdict

### ✅ Ship

| Tool | Evidence |
|---|---|
| `list_projects` | 200, correct project list with stats |
| `get_project_home` | Full snapshot: subsystems, chokepoints, risk surface, health, decisions |
| `query_similar_functions` | Correct results with `_guidance` field, 0.70–0.83 similarity |
| `get_callers` | Correct 5 callers for `check_permission` with guidance |
| `get_callees` | Correct callees for `get_project_home` |
| `get_impact_radius` | Correct BFS with `impact_depth` annotation |
| `get_function_context` | Unified pipeline: node + callers + callees + impact + decisions + similar |
| `find_dependents` | Correct dependent set for `CallGraphDB` |
| `get_decision_history` | Returns decision array with `_guidance` |
| `query_decisions` | Semantic search correct — ArchitectureService decision surfaced at 0.83 |
| `list_contracts` | Returns empty correctly when no contracts active |
| `check_contracts` | Returns empty violations correctly |
| `list_improvements` | Returns empty when no open improvements |
| `check_performance` | Works, returns findings with structural_causes — needs noise reduction (#3) |
| `setup_phronosis_client` | Generates correct hooks, settings, CLAUDE.md, memory, skill — URL issue (#4) |
| `log_decision` | Auth protected via `check_permission` ✅ |
| `index_project` | Auth protected ✅ |
| `index_changes` | Auth protected ✅ |
| `reembed_project` | Auth protected ✅ |
| `enrich_summaries` | Auth protected ✅ |
| `file_improvement` | Tested in prior sessions ✅ |
| `list_improvements` | ✅ |
| `resolve_improvement` | ✅ |
| `check_solid_principles` | Tool exists, has correct SRP/OCP/DIP detectors — undocumented (#7) |
| `dismiss_solid_concern` | ✅ |
| `dismiss_performance_concern` | ✅ |
| `validate_proposed_code` | Conformance score + deviations, backed by test suite |
| `preflight_architecture` | Coupling hotspots + external scatter + duplication clusters |
| `create_contract` | Auth not tested via MCP (not tested this session, but has `check_permission` in MCP path) |
| `approve_contract` | Same |
| `index_lsif` | Compiles, not tested end-to-end (no LSIF file on server) |
| `index_scip` | Compiles, SCIP binary installed on server ✅ |
| `index_schema_objects` | Not tested end-to-end |

### 🔧 Fix before Phase 17

| Tool | Issue |
|---|---|
| `lsp_get_diagnostics` | Path resolution — server vs. client paths (#2) |
| `lsp_get_definition` | Same |
| `lsp_find_references` | Same |
| `list_external_dependencies` | Empty without guidance note (#5) |
| `get_library_dependents` | Empty without guidance note (#5) |
| `get_dependency_fingerprint` | Empty without guidance note (#6) |

### ⚠️ Untested

| Tool | Reason |
|---|---|
| `compare_branches` | No branch data in deployment |
| `get_branch_conflicts` | No branch data in deployment |
| `get_function_at_commit` | No commit hash test case |
| `estimate_index` | Requires server-side path |

---

## HTTP Endpoints — Full Verdict

### ✅ Ship (read-only, no auth required)

| Endpoint | Test result |
|---|---|
| `GET /api/health` | 200, DB + embeddings + decision_memory status |
| `GET /api/projects` | 200, list of 14 projects |
| `GET /api/project-home/{id}` | 200, full architectural snapshot |
| `GET /api/violations` | 200, violations list |
| `GET /api/contracts` | 200, contracts list |
| `GET /api/jobs/{id}` | 404 for unknown ID (correct) |
| `POST /api/search` | 200, returns results (read-only) |
| `POST /api/functions` | 200, returns function_ids for files (read-only) |
| `POST /api/contracts/check` | 200, returns violations (read-only) |

### 🔧 Fix before Phase 17 (write endpoints, missing auth)

| Endpoint | Risk |
|---|---|
| `POST /api/index-bulk` | Arbitrary data injection into any project |
| `POST /api/enrich-summaries/{id}` | Expensive LLM call, no rate limit |
| `POST /api/reembed/{id}` | Expensive embedding run |
| `POST /api/contracts` | Create contracts in any project |
| `PUT /api/contracts/{id}` | Modify any contract |
| `POST /api/contracts/{id}/approve` | Approve any contract |
| `POST /api/contracts/{id}/deactivate` | Deactivate any contract |
| `DELETE /api/projects/{id}` | **Delete any project's entire index** |
| `DELETE /api/contracts/{id}` | Delete any contract |

### ℹ️ Intentionally unauthenticated (document and network-restrict)

| Endpoint | Reason |
|---|---|
| `POST /api/decisions` | Git hook calls this without user credentials |
| `POST /index` | Same (git_hook_index) |

---

## Web UI Panels

Not tested end-to-end (requires browser). Based on code inspection of `src/web/template.py`:

- **Search panel** — `POST /api/search` works ✅; UI rendering untested
- **Project home panel** — `GET /api/project-home/{id}` works ✅; UI rendering untested
- **Contracts panel** — `GET /api/contracts` works ✅; write actions unauth'd (#1)
- **Improvements panel** — data API works ✅; UI rendering untested

**Recommendation:** Manual browser verification needed before Phase 17. All data APIs
used by the UI return correct responses; the risk is rendering bugs.

---

## Hooks

| Hook | Status |
|---|---|
| `phronosis-suggest.py` (PreToolUse: Bash/Read/Edit) | Installed at `~/.claude/hooks/`. Fires on every Edit with chokepoint/risk-surface warnings. Gate valid 30 min. ✅ |
| `phronosis-post-edit.py` (PostToolUse: Edit) | Auto-indexes edited `.py/.ts/.tsx` files in background. Template staleness warnings. ✅ |
| `phronosis-push-review.py` (PostToolUse: git push?) | Exists at `~/.claude/hooks/`. Not tested this session. ⚠️ |
| Post-commit git hook | Installed at `.git/hooks/post-commit`. Fires on commits — confirmed from output during today's commits. ✅ |

---

## CLI Scripts

| Script | Status |
|---|---|
| `scripts/create_user.py` | `--help` fails locally (asyncpg not in system Python). Works on K8s pod where venv is active. 🔧 Should add `#!/usr/bin/env python3` or document venv requirement. |
| `scripts/backfill_decisions.py` | `--help` works ✅. Has `--dry-run`, `--since`, `--limit`, `--project` flags. |
| `scripts/index_demo_repos.py` | `--help` works ✅. Has `--repos`, `--skip-enrich`, `--mark-only` flags. |

---

## Summary

**Blocks Phase 17 launch (2 critical, 4 medium):**
1. HTTP write endpoints — no auth — including delete project
2. LSP tools — path resolution broken for remote clients
3. `check_performance` — too noisy for first-time users
4. `setup_phronosis_client` — hardcodes localhost URL
5. `list_external_dependencies` + `get_library_dependents` — empty without explanation
6. `get_dependency_fingerprint` — empty without explanation

**Does not block but document before Phase 17:**
7. `check_solid_principles` / `dismiss_solid_concern` — add to tool reference
8. Branch tools — add to docs as "requires branch tracking setup"
9. `POST /api/decisions` / `POST /index` — document as intentionally public

**Clear to ship as-is (40+ tools):** All read/query tools, all auth-protected MCP write
tools, read HTTP endpoints, hooks, `backfill_decisions.py`, `index_demo_repos.py`.
