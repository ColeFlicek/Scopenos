# Phronosis MCP Tool Reference

Internal reference. Every tool, every parameter, one working example against an indexed demo repo.
Promoted to `docs.phronosis.dev` in Phase 17 with pricing/limitation sections redacted.

---

## Discovery tools

### `list_projects`
List all indexed projects with node count, edge count, and last-indexed timestamp.

**Parameters:** none

**Example:**
```
list_projects()
→ [{"id": "django", "name": "django", "node_count": 42988, "edge_count": 189763, ...}]
```

---

### `get_project_home(project_id)`
Full architectural snapshot in one call: subsystems, wiring diagram, chokepoints, entry points, risk surface, health, recent decisions. **Call this first at every session start.**

**Parameters:**
- `project_id` — string, required

**Example:**
```
get_project_home("pytest")
→ {
    "function_count": 6578,
    "subsystems": [{"name": "src._pytest", "function_count": 2199, ...}],
    "chokepoints": [{"id": "src._pytest.pytester.LineMatcher.fnmatch_lines", "caller_count": 1750}],
    "health": {"top_knowledge_gaps": [...], "risk_detection_mode": "structural_heuristic_no_decisions"},
    "recent_decisions": []
  }
```

---

### `query_similar_functions(snippet, project_id?, top_k?)`
Semantic search — find functions by what they do, not what they're named. Returns top-k results with similarity scores and a `_guidance` field.

**Parameters:**
- `snippet` — natural language description or code fragment, required
- `project_id` — limit to one project; omit to search all, optional
- `top_k` — default 10, optional

**Example:**
```
query_similar_functions("HTTP session cookie authentication middleware", project_id="requests", top_k=5)
→ {"results": [{"id": "tests.test_requests.TestRequests.test_DIGEST_AUTH_SETS_SESSION_COOKIES",
                "similarity": 0.742, "file": "tests/test_requests.py", ...}]}
```

---

### `get_function_context(function_name, project_id?)`
Unified pipeline for a single function: node metadata + callers + callees + impact radius + decision history + similar functions. Use this when you have a function name and want everything about it.

**Parameters:**
- `function_name` — exact function ID or name, required
- `project_id` — optional scoping

---

### `get_callers(function_name, project_id?)`
All functions that call the named function, with file, module, and signature.

**Example:**
```
get_callers("Session", project_id="requests")
→ {"callers": [{"id": "tests.test_requests.TestRequests.test_session_...", "caller_count": 58}]}
```

---

### `get_callees(function_name, project_id?)`
All functions called by the named function, with `is_external` flag to distinguish library calls.

---

### `get_impact_radius(function_name, depth?, project_id?)`
Recursive BFS of everything that depends on this function. Returns full dependency tree annotated with `impact_depth`.

**Parameters:**
- `function_name` — required
- `depth` — default 2; use 1 for chokepoints (depth=2 can return 70+ functions for heavily-called nodes)
- `project_id` — optional

**Example:**
```
get_impact_radius("check_permission", depth=2, project_id="phronosis")
→ {"impact_radius": [...], "total_impacted": 18}
```

---

### `find_dependents(function_name, project_id?)`
All functions that transitively depend on this function (callers-of-callers). Equivalent to `get_impact_radius` but flat list.

---

## Decision memory tools

### `get_decision_history(function_name, project_id?)`
All logged architectural decisions linked to this function — why it was designed this way, what was rejected.

**Note:** Returns empty `[]` if `log_decision` has never been called for this function. `risk_detection_mode: "structural_heuristic_no_decisions"` in `get_project_home` is the signal that no decisions are logged yet.

---

### `query_decisions(query_text, project_id?)`
Semantic search over decision memory. Finds decisions by topic, not by function name.

**Example:**
```
query_decisions("why did we choose ContextVar over threading.local")
→ [{"description": "Auth uses ContextVar for per-request user...", "similarity": 0.83}]
```

---

### `log_decision(project_id, type, description, linked_function_ids?, rejected_alternatives?, trigger?)`
Record a significant architectural decision. Call after any non-obvious choice, after every git push, and whenever an approach is rejected.

**Parameters:**
- `type` — `"Architectural"` | `"Design"` | `"Implementation"` | `"Patch"`
- `description` — what changed AND why (not just the commit message)
- `linked_function_ids` — list of function IDs this decision applies to
- `rejected_alternatives` — what was considered and not done
- `trigger` — `"git:<short-hash>"` for commit-linked decisions

**Auth:** requires valid API key — owner or write access to project.

---

## Contract tools

### `list_contracts(project_id?)`
List active contracts. Returns empty array if none.

---

### `create_contract(project_id, title, natural_language, function_ids?)`
Generate a contract draft from a plain-English rule. Uses Claude Haiku to parse the rule into violation/compliance examples and a structural expression.

**Auth:** requires valid API key.

---

### `approve_contract(contract_id)`
Embed examples and activate the contract. After approval, `check_contracts` will flag violations.

**Auth:** requires valid API key.

---

### `check_contracts(project_id, function_ids?)`
Check functions against all active contracts. Returns violations. Called automatically by the post-commit hook.

---

### `list_improvements(project_id?)`
List open improvement suggestions filed by `file_improvement`.

---

### `file_improvement(project_id, function_id, description, suggested_fix?)`
File a suggested improvement for a function. Stored in decision memory; visible in web UI improvements panel.

---

### `resolve_improvement(improvement_id, resolution_notes, status)`
Close an improvement as `resolved`, `dismissed`, or `wont_fix`.

---

## Performance tools

### `check_performance(project_id, exclude_test_files?)`
Run all performance detectors against a project.

**Parameters:**
- `project_id` — required
- `exclude_test_files` — default `true`; set to `false` to include test code

**Detectors:**
- `correlated_join_aggregate` — SQL Cartesian product before GROUP BY
- `n_plus_one` — loop + DB call inside it
- `quadratic_expansion` — O(n²) composition via embedding similarity
- `external_call_in_loop` — HTTP/AI API calls serialized inside a loop
- `sequential_awaits` — independent async operations that could use `gather()`

**Example:**
```
check_performance("requests")
→ {"total": 3, "new": 2, "acknowledged": 1, "findings": [...]}
```

---

### `dismiss_performance_concern(project_id, function_id, reason)`
Acknowledge a finding as intentional. It will appear with `status: "acknowledged"` on future runs.

---

### `check_solid_principles(project_id)`
Run SRP/OCP/DIP detectors. Undocumented in Phase 16 — add to Phase 17 docs.

### `dismiss_solid_concern(project_id, function_id, reason)`
Acknowledge a SOLID finding.

---

## Dependency fingerprint tools

### `get_dependency_fingerprint(project_id)`
Latest snapshot of all external library symbols in use, with diff from previous snapshot. Returns guidance note if no SCIP data is available.

### `list_dependency_fingerprint_history(project_id, limit?)`
All fingerprint snapshots, newest first.

### `get_dependency_fingerprint_at(fingerprint_id)`
Full snapshot for a specific point in time.

### `compare_dependency_fingerprints(fingerprint_id_a, fingerprint_id_b)`
Diff two historical snapshots.

### `list_external_dependencies(project_id)`
All external library symbols called by this project, grouped by library. Returns guidance note if no SCIP data.

### `get_library_dependents(library_name, project_id)`
All internal functions that call any symbol in the given library. Returns guidance note if no SCIP data.

**Note:** All dependency fingerprint tools require SCIP augmentation. Run `index_scip` or `index_project` with `scip-python` installed to populate external symbol data.

---

## Indexing tools

### `index_project(path, project_id?)`
Full index of a project from server-side filesystem path. Enqueues a background job; returns `job_id`.

**Auth:** requires valid API key.

### `index_changes(changed_files, file_contents, project_root?, project_id?)`
Incremental index — update only the changed files. Synchronous (fast). Called by the post-edit hook.

**Auth:** requires valid API key.

### `index_scip(path, project_id?)`
Ingest a SCIP index file (JSON format) to populate external dependency data and type-resolved call edges.

### `index_schema_objects(project_id)`
Detect and index database schema objects (tables, columns, cardinality classes) for performance analysis.

### `reembed_project(project_id)`
Force re-embedding of all functions. Use after changing the embedding model.

**Auth:** requires valid API key.

### `enrich_summaries(project_id, limit?, force?)`
Generate LLM summaries for functions that fell back to the large model (no docstring). Enqueues background job.

**Auth:** requires valid API key.

---

## Setup tool

### `setup_phronosis_client(project_id, project_root, server_url?)`
Generate all client-side artifacts for a new project: post-commit hook, Claude Code settings, CLAUDE.md section, memory files, and the `acip-workflow` skill.

**Parameters:**
- `project_id` — required
- `project_root` — absolute path to the project root
- `server_url` — the Phronosis server URL as seen from the client (e.g. `http://100.71.88.106:3004`). If omitted, falls back to `PHRONOSIS_URL` env var then `http://localhost:3004`.

**Important:** Always pass `server_url` explicitly when connecting to a remote instance. The tool reads its own `PHRONOSIS_URL` (the server's internal address), which is wrong for remote clients.

---

## Branch tools (requires branch tracking setup)

### `compare_branches(project_id, branch_a, branch_b)`
Diff call graphs between two branches.

### `get_branch_conflicts(project_id)`
Surface functions modified on multiple branches simultaneously.

**Note:** These tools require the project to be indexed with branch tracking enabled. Not tested against current deployment — no branch data exists yet.

---

## Untested / limited tools

| Tool | Status |
|---|---|
| `get_function_at_commit` | Requires commit hash — no test case |
| `estimate_index` | Requires server-side path access |
| `index_lsif` | Compiles; no LSIF file available for testing |
| `compare_branches` / `get_branch_conflicts` | No branch data in deployment |
