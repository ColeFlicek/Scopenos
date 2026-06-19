# Known Limitations

Honest internal notes. Not for public docs. Review before Phase 17 to decide what to disclose vs. what to fix.

---

## Analysis limitations

### Decision memory is empty for most functions
`get_decision_history` returns `[]` for virtually every function because `log_decision` has not been called consistently, and `backfill_decisions.py` has never been run against the production indexes. The `risk_detection_mode: "structural_heuristic_no_decisions"` flag in `get_project_home` is the indicator.

**Impact:** Risk scoring degrades. The system falls back to structural heuristics (call count, churn) instead of knowledge-aware risk detection. Agents will not see why things were designed a certain way.

**Fix:** Run `backfill_decisions.py` against all indexed projects. Enforce `log_decision` calls in the post-commit hook (currently the hook logs commit-level decisions but doesn't always link `linked_function_ids`).

---

### check_performance flags test code without `exclude_test_files=True`
The default is now `exclude_test_files=True`, but the tool historically flagged test helper functions as N+1 and external-call-in-loop patterns. In large repos (scikit-learn, django, astropy) there may be legitimate-looking test patterns that still trigger.

**Impact:** First-time users on large repos may see noise. Trust erodes.

**Fix:** Defaults now exclude test files. Users can opt in with `exclude_test_files=False`. Pre-dismiss known false positives in batch/executemany code for each indexed demo repo.

---

### SCIP data is empty for all indexed projects
`list_external_dependencies`, `get_library_dependents`, and `get_dependency_fingerprint` all return empty results with a guidance note. SCIP augmentation (`index_scip`) has never been run on any demo repo.

**Impact:** Dependency fingerprint and library migration planning tools are not functional for any current demo repo. A user querying django for "which functions use requests?" gets an empty result.

**Fix:** Run `scip-python index` + `index_scip` on each demo repo. One-time per repo. Estimated 2-4 minutes per repo.

---

### `app.src.*` namespace duplication in Phronosis's own index
Functions in Phronosis itself appear under both `src.module.function` and `app.src.module.function` in the call graph. This is a Docker build artifact where the container's working directory creates a duplicate import path.

**Impact:** `get_callers` for Phronosis functions may return half the actual callers. Subsystem counts are inflated.

**Fix:** Pending cleanup — remove the `app.src.*` entries from the call graph. Do not expose Phronosis's own index as a demo repo until this is resolved.

---

### Branch tools are untested
`compare_branches` and `get_branch_conflicts` have no branch data in the current deployment. The tools compile and the schema supports branch tracking, but end-to-end correctness is not verified.

**Impact:** If a user tries these tools, they will return empty results or errors.

**Recommendation:** Document as "requires branch tracking setup" in Phase 17 docs. Do not feature them on the landing page.

---

### `estimate_index` requires server-side file access
The tool accepts an absolute path on the Phronosis server filesystem, not a remote path. Users on local machines cannot use it without either SSHing in or uploading files first.

**Impact:** The "how much will this cost?" pre-flight check doesn't work for remote clients.

**Fix:** Accept file contents inline (like `index_changes`) instead of a path. Low priority.

---

## Scale ceilings

| Constraint | Current limit | Notes |
|---|---|---|
| asyncpg connection pool | max=10 | Raise to 50 before high-concurrency load |
| HNSW index | Not applied | Apply before >100K embeddings for query performance |
| enrich_summaries per job | 2,000 functions cap | Prevents runaway LLM costs; configurable |
| RQ job queue | 1 concurrent job per user | Redis rate key per user_id |
| Django index size | 42,988 nodes, 189,763 edges | Largest demo repo; `get_project_home` returns 141KB — hits inline MCP result limit |

---

## False positive patterns by detector

### n_plus_one
- **False positive:** Loops over a small fixed-size collection (e.g. `for col in ["a", "b", "c"]`) that happens to call a DB function. Cardinality gate doesn't catch it if the collection literal isn't recognized.
- **False positive:** Test setup functions that insert rows in a loop — intentional, not a performance concern.

### external_call_in_loop
- **False positive:** Batch API clients that are designed to be called in a loop (e.g. OpenAI SDK with automatic retry — each call is independent and can't be batched further).
- **False negative:** Loops hidden inside generator expressions (`sum(f(x) for x in data)` where `f` makes an HTTP call).

### sequential_awaits
- **False positive:** Sequential awaits that are intentionally ordered (e.g. "first acquire lock, then use resource"). The detector does not model dependencies between awaits.

### quadratic_expansion (embedding-based)
- Requires function embeddings to be indexed. Returns no findings if embeddings are empty.
- Similarity threshold is calibrated on Python web service code. May over-flag scientific computing code where O(n²) operations are intentional (numpy outer products, pairwise distance matrices).

---

## Unsupported language patterns

### Python
- Decorators that change function signatures are not modeled (the decorator is tracked as a call edge, but parameter names reflect the wrapped function)
- `__getattr__`-based dynamic dispatch is invisible to the call graph
- Metaclass-generated methods are not indexed

### JavaScript / TypeScript
- Anonymous functions (arrow functions, `function()`) are indexed but have auto-generated IDs that may not match between index runs if the file changes significantly
- Dynamic `require()` calls are not resolved to their target module

### Generic fallback parsers (Zig, Groovy, Perl, etc.)
- Body text is captured but call edges are not extracted (no tree-sitter grammar). `get_callers` / `get_callees` returns empty for these functions.
- Structural layer is `"generic"` — use this to filter results when precision matters.

---

## What a paying user might hit first

1. **"get_decision_history returns nothing"** — see decision memory section above
2. **"query_similar_functions returns unrelated results"** — happens when functions have no docstrings and enrichment hasn't been run; body text embeddings are lower quality
3. **"check_performance says my test helpers are N+1"** — default now excludes test files, but may still occur for test files outside a `tests/` directory
4. **"list_external_dependencies returns empty"** — SCIP not run; guidance note explains
5. **"get_project_home returns 141KB for django"** — hits MCP inline result limit; jq extraction is the workaround
