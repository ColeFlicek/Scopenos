# Scopenos Database Health Report

_Generated: 2026-06-21 14:46 UTC — 1020ms — DB: postgresql://acip:***@172.21.0.1/acip — Size: 3539 MB_

> Focused on project: `django`

## Schema Overview

_Project-scoped tables filtered to `django`. Global tables show totals._

| Table | Rows | Scope |
|---|---:|---|
| `edges` | 189,763 | project: `django` |
| `nodes` | 42,988 | project: `django` |
| `function_embeddings` | 42,988 | project: `django` |
| `schema_object_embeddings` | 6,960 | project: `django` |
| `commit_function_changes` | 3,283 | global |
| `embedding_cache` | 737 | global |
| `decision_functions` | 669 | global |
| `decisions` | 0 | project: `django` |
| `decision_embeddings` | 139 | global |
| `dependency_fingerprints` | 1 | project: `django` |
| `contract_examples` | 32 | global |
| `projects` | 15 | global |
| `project_home_snapshots` | 1 | project: `django` |
| `demo_projects` | 14 | global |
| `contracts` | 4 | global |
| `api_keys` | 3 | global |
| `users` | 1 | global |
| `project_access` | 1 | global |
| `contract_violations` | 0 | project: `django` |
| `module_patterns` | 0 | project: `django` |
| `agent_improvements` | 0 | project: `django` |
| `branch_function_changes` | 0 | project: `django` |

## Project Inventory

| ID | Name | Nodes | Edges | Last Indexed | Demo |
|---|---|---:|---:|---|:---:|
| `django` | django | 42,988 | 189,763 | 2026-06-16T16:55 | ✓ |

## Per-Project Coverage


### `django` — django

**Benchmark readiness:** 🟡 **Partial** — embeddings present but coverage low

| Metric | Count | Coverage | Bar |
|---|---:|---:|---|
| Total nodes (all) | 42,988 | — | |
| Internal nodes | 42,988 | — | |
| External stubs | 0 | — | |
| With embedding | 42,988 | 100.0% | `████████████████████` |
| With LLM summary | 12,075 | 28.1% | `██████░░░░░░░░░░░░░░` |
| With docstring | 7,575 | 17.6% | `████░░░░░░░░░░░░░░░░` |
| With body source | 32,234 | 75.0% | `███████████████░░░░░` |
| With body_hash | 32,234 | 75.0% | `███████████████░░░░░` |
| Orphan nodes | 6,972 | 16.2% | `███░░░░░░░░░░░░░░░░░` |
| Decisions logged | 0 | — | |
| Edges | 189,763 | — | |

**Node types (internal):** `method` 29,809, `class` 10,754, `function` 2,425

**Enrichment backlog:** 21,987 nodes have source body but no summary — estimated $6.60 to enrich (@ $0.30/1k).

**Top subsystems by function count:**

| Subsystem | Functions |
|---|---:|
| `django.db` | 3,817 |
| `django.contrib` | 3,810 |
| `tests.template_tests` | 1,992 |
| `tests.forms_tests` | 1,441 |
| `tests.migrations` | 1,347 |
| `tests.admin_views` | 1,252 |
| `tests.auth_tests` | 1,248 |
| `django.core` | 1,164 |
| `tests.invalid_models_tests` | 999 |
| `tests.gis_tests` | 956 |
| `tests.utils_tests` | 912 |
| `tests.postgres_tests` | 820 |

## Contracts

- Active: 4
- Draft: 0
- Total: 4

## Benchmark Readiness Summary

| Project | Embedding % | Summary % | Verdict |
|---|---:|---:|---|
| `django` | 100.0% | 28.1% | 🟡 Partial |

_Embedding coverage drives `query_similar_functions`. Summary coverage enriches result context. Both must be high for Path B to outperform Path A._

## Tool Reference

_What each Scopenos MCP tool returns. All tools require `project_id`._

### `get_project_home(project_id)`
Architectural snapshot. Call this first every session.

```json
{
  "subsystems": [
    {
      "name": "django.db",
      "function_count": 3817,
      "anchor": "django.db.models.Model",
      "anchor_summary": "Base class for all ORM model instances",
      "top_functions": [
        {"id": "django.db.models.Model.__eq__", "caller_count": 38}
      ]
    }
  ],
  "connections": [
    {"from": "django.db", "to": "django.db.models.sql", "edge_count": 84}
  ],
  "chokepoints": [
    {"id": "django.db.models.Model.save", "caller_count": 201}
  ],
  "recent_decisions": []
}
```

### `query_similar_functions(snippet, project_id, top_k=10)`
Semantic search — find functions by concept, not name. Use when you don't know the exact symbol.

```json
{
  "results": [
    {
      "id": "django.db.models.Model.__eq__",
      "name": "__eq__",
      "summary": "Compare model instances by pk",
      "file": "django/db/models/base.py",
      "signature": "def __eq__(self, other)",
      "similarity": 0.94
    }
  ],
  "_guidance": {"next_step": "call get_impact_radius on the top result id"}
}
```

### `get_impact_radius(function_name, project_id, depth=2)`
BFS outward from a function — what breaks if this changes. Also returns `co_change_hints` with three signals: protocol gaps, semantic siblings not in the call graph, and git co-change history.

```json
{
  "impact_radius": [
    {"id": "django.db.models.Model.__eq__", "impact_depth": 0,
     "file": "django/db/models/base.py", "signature": "def __eq__(self, other)"},
    {"id": "django.db.models.Model.pk",    "impact_depth": 1, "file": "..."}
  ],
  "co_change_hints": [
    {
      "type": "protocol_completeness",
      "message": "`__eq__` defined but `__hash__` not found on django.db.models.Model. Python requires both.",
      "suggested_id": "django.db.models.Model.__hash__",
      "action": "add"
    },
    {
      "type": "semantic_sibling",
      "message": "`__eq__` on AbstractUser is semantically similar but not reachable via call edges.",
      "id": "django.contrib.auth.models.AbstractUser.__eq__",
      "file": "django/contrib/auth/models.py",
      "similarity": 0.91
    },
    {
      "type": "co_change_history",
      "message": "`__lt__` has changed together with `__eq__` 7 times in git history — likely needs a parallel update.",
      "id": "django.db.models.Model.__lt__",
      "co_change_count": 7
    }
  ]
}
```

**`co_change_hints` signal types:**

| Type | Source | Fires when |
|---|---|---|
| `protocol_completeness` | Hardcoded dunder pairs | `__eq__` defined but `__hash__` missing on the same class |
| `semantic_sibling` | Embedding similarity | Function is conceptually similar but unreachable via call edges |
| `co_change_history` | Git commit history | Function appears in the same commits ≥3 times (`commit_function_changes` table) |

_`co_change_history` is silent when `commit_function_changes` is empty — run `scripts/backfill_cochange.py` first._

### `get_callers(function_name, project_id)`
Every function that calls this one — with file and signature.

```json
{
  "callers": [
    {
      "id": "django.test.TestCase.assertQuerysetEqual",
      "name": "assertQuerysetEqual",
      "file": "django/test/testcases.py",
      "signature": "def assertQuerysetEqual(self, qs, values, ...)"
    }
  ],
  "_guidance": {"next_step": "read the callers to understand usage contracts"}
}
```

### `get_callees(function_name, project_id)`
Every function this one calls — `is_external` flags stdlib/third-party symbols.

```json
{
  "callees": [
    {"id": "django.db.models.sql.compiler.SQLCompiler.execute_sql",
     "name": "execute_sql", "is_external": false,
     "file": "django/db/models/sql/compiler.py"},
    {"id": "external.builtins.hash",
     "name": "hash", "is_external": true, "file": ""}
  ],
  "_guidance": {"next_step": "check is_external=false callees for cascading impact"}
}
```

### `get_subsystem_detail(project_id, subsystem_name)`
Full function list and wiring for one subsystem. Call before reading any file in that subsystem — avoids reading large files blind.

```json
{
  "subsystem": "tests.model_tests",
  "anchor_summary": "Base model test fixtures and assertion helpers",
  "top_functions": [
    {"id": "tests.model_tests.ModelTests.test_eq",
     "summary": "Tests Model.__eq__ with pk comparison",
     "caller_count": 0}
  ],
  "connections": [
    {"from": "tests.model_tests", "to": "django.db.models", "edge_count": 47}
  ]
}
```

### `get_decision_history(function_name, project_id)`
Every logged decision linked to this function — architectural, design, implementation, and patch. Run before editing any function you didn't write.

```json
{
  "decisions": [
    {
      "id": "abc-123",
      "type": "Implementation",
      "description": "Changed __eq__ to compare by pk only — see issue #13606",
      "rejected_alternatives": "Considered value equality but breaks ORM identity assumptions",
      "trigger": "git:bb45e94",
      "created_at": "2026-06-20T00:00:00"
    }
  ],
  "_guidance": {"next_step": "check rejected_alternatives before making changes"}
}
```

### Field glossary

| Field | Meaning |
|---|---|
| `id` | Fully-qualified dotted symbol: `module.Class.method` |
| `impact_depth` | BFS distance from the target function (0 = the function itself) |
| `is_external` | `true` = stdlib or third-party; not in the project's call graph |
| `caller_count` | How many other functions call this one — proxy for change risk |
| `similarity` | Cosine similarity 0–1 from the embedding search |
| `co_change_hints` | Functions likely needing a parallel change — three signals: `protocol_completeness`, `semantic_sibling`, `co_change_history` |
| `_guidance` | Suggested next tool call based on what was returned |
