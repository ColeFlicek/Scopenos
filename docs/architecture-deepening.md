# Architecture Deepening — Session Notes

This document explains every architectural change made in one session, why
each route was taken over the alternatives, and what principle it serves.
Written as a teaching resource so you can apply the same reasoning to future
work, not just repeat the specific pattern.

---

## Vocabulary (used precisely throughout)

| Term | Meaning |
|---|---|
| **Module** | Anything with an interface and an implementation (function, class, package) |
| **Interface** | Everything a caller must know: types, invariants, error modes, ordering |
| **Depth** | Leverage at the interface — lots of behaviour behind a small surface |
| **Shallow module** | Interface nearly as complex as the implementation |
| **Seam** | Where an interface lives; a place behaviour can be swapped without editing in place |
| **Deletion test** | If you deleted this module, would complexity vanish or reappear in callers? |
| **Locality** | When change, bugs, and knowledge are concentrated in one place |

---

## Change 1 — Extract `ArchitectureAnalyzer` from `CallGraphDB`

### What was wrong

`CallGraphDB.get_project_home_data()` was 161 lines of analytical code inside a class
named after database operations. It grouped nodes into subsystems, ranked knowledge
gaps, classified entry points by HTTP decorator patterns, and computed risk surfaces.
None of that is storage logic.

**Deletion test applied:** remove `get_project_home_data` and the DB class is still
complete and correct — nothing storage-related breaks. The complexity of the analysis
doesn't vanish; it reappears, but it now belongs in an analysis class where it's
visible, named, and testable.

**Specific breach:** HTTP framework patterns (`@app.route`, `@router.get`, etc.) were
hardcoded inside `storage.py` at line 1025. To support a new framework, you'd edit a
database file. That's the smell that made this obvious.

### What changed

- `src/call_graph/models.py` (new) — `GraphData` dataclass: all raw data fetched from
  SQL, typed. `ArchitectureSnapshot` dataclass: the typed output.
- `src/analysis.py` (new) — `ArchitectureAnalyzer(http_patterns=DEFAULT_HTTP_PATTERNS)`.
  Pure sync class. Takes `GraphData`, returns `ArchitectureSnapshot`. No imports from
  anything that touches a database.
- `src/call_graph/storage.py` — `fetch_graph_data(project_id) -> GraphData` (the SQL
  fetches). `get_project_home_data()` reduced to 5 lines: fetch → analyze → save
  snapshot → return dict.

### Why Option A (pure data bundle) over Option B (analyzer holds a DB reference)

Option B keeps I/O in the analyzer. You'd still need a real database to test the
heuristics. Option A makes the seam explicit: the storage layer fetches raw data,
the analyzer transforms it, the wrapper commits the side effect (snapshot save). Each
piece is testable in isolation with plain Python dicts.

### Why `GraphData` and `ArchitectureSnapshot` as typed dataclasses

A plain dict works but documents nothing. A caller reading
`ArchitectureAnalyzer.snapshot(data: GraphData) -> ArchitectureSnapshot` immediately
knows what feeds the analyzer and what comes out. The interface is the documentation.

### Why `http_patterns` as a constructor parameter with a default

The pattern list is the only configuration the analyzer needs. Making it a constructor
parameter costs three characters and gains: (1) tests can pass a minimal list, (2)
projects using non-standard frameworks can override it, (3) the patterns move out of
storage.py into a file named `analysis.py` where they make semantic sense.

We rejected a global constant (can't override per-project) and a config file (overkill
for 15 strings with a sensible default).

### Roadmap connection

The `ArchitectureAnalyzer` is now the natural home for two ROADMAP features:
- **Temporal Semantic Graph** — track when a function's embedding crosses a distance
  threshold. This is an analyzer-level concept: compare `GraphData` from two timestamps.
- **Intent-Structure Gap** — compare decision history embedding vs. implementation
  embedding. Same layer.

Neither would have fit cleanly inside `CallGraphDB`.

---

## Change 2 — Extract `EmbeddingPipeline` from `EmbeddingStore`

### What was wrong

`EmbeddingStore.upsert_chunks()` had two problems:

**Problem A — wrong owner for node writes.** `UPDATE nodes SET summary, embedding_model`
was being called from inside a class named "Store." The `nodes` table is `CallGraphDB`'s
domain. EmbeddingStore was reaching through the abstraction with `self._db._db` (the
raw aiosqlite connection) to write to a table it doesn't own.

**Problem B — routing strategy buried in storage.** The two-tier logic — documented
functions use the small model; undocumented functions fall back to the large model — is
a strategic decision. It's the thing most likely to change as the system evolves (add a
third tier, change the threshold, swap the model). It was fused into a storage method
where you'd never look for it.

`enrich_summaries` had the same two problems.

### What changed

- `src/embeddings/pipeline.py` (new) — `EmbeddingPipeline(db, store)`. Owns routing and
  orchestration. `upsert_chunks` classifies chunks, calls `store._embed_batch()` /
  `store._embed_batch_large()`, then calls `db.update_node_embedding_meta()` and
  `store.upsert_vector()`. `enrich_summaries` same pattern.
- `src/embeddings/embedder.py` — `upsert_chunks` and `enrich_summaries` removed.
  `upsert_vector(node_id, vector, project_id)` added — encapsulates the DELETE+INSERT
  vec0 pattern. The `self._db._db` accesses for vec0 tables remain (EmbeddingStore owns
  those tables; writing to them is correct).
- `src/call_graph/storage.py` — `update_node_embedding_meta()`, `commit()`,
  `get_nodes_needing_enrichment()`, `count_nodes_by_model()` added. Node metadata writes
  now go through the right owner.
- `src/indexer.py` — receives `EmbeddingPipeline` instead of `EmbeddingStore`.
- `src/server.py` — `pipeline` added to service container; `enrich_summaries` routes to
  `svcs.pipeline`.

### Why EmbeddingPipeline is a drop-in for Indexer

Indexer needs: `upsert_chunks`, `delete_by_ids`, `delete_by_file`, `get_summaries`. The
pipeline implements the first and delegates the rest to EmbeddingStore. Indexer talks
to one object, doesn't know whether it's a store or a pipeline.

Rejected alternative: keep `EmbeddingStore` as the Indexer interface and have it call
the pipeline internally. This would hide the seam — the routing heuristic would still
be invisible to callers. The pipeline being a first-class object makes the architecture
legible.

### Why `_embed_batch` stays private in EmbeddingStore

The pipeline calls `store._embed_batch()` from within the same package (`src/embeddings/`).
Cross-package private access is a code smell; same-package access to a private method is
a reasonable way to give a sibling narrower access than the full public interface. We
chose not to rename to `embed_batch` (public) because these methods are not part of the
interface callers outside `embeddings/` should use — they should use the pipeline.

### The `self._db._db` accesses that remain

After the extraction, EmbeddingStore still accesses `self._db._db` for vec0 table
operations (`query_similar`, `delete_by_ids`, `upsert_vector`). This is acceptable
because EmbeddingStore owns the vec0 tables — they don't exist without it, and only it
writes to them. The breach that mattered was writing to `nodes`, which is CallGraphDB's
domain. Ownership determines which `_db._db` accesses are legitimate.

---

## Change 3 — `ContractRule` extraction from `ContractManager`

### What was wrong

`_check_structural()` and `_check_structural_for_function()` in `ContractManager` had
two separate problems:

**Problem A — abstraction breach.** Both used `self._db._db.execute(...)` for:
1. `SELECT DISTINCT caller_id FROM edges` — querying CallGraphDB's edges table directly
2. `SELECT callee_id FROM edges WHERE caller_id = ?` — same, and this query already
   exists as `CallGraphDB.get_callees()`

Pattern matching was duplicating traversal logic that exists in storage.

**Problem B — untestable rule logic.** The prohibited-pattern matching (is this callee
name in the prohibited list? does the required callee appear?) was inline in a for-loop
inside an async method. You can't test the matching logic without a database, an active
contract, and project nodes.

### What changed

- `src/contracts/rule.py` (new) — `ContractRule` dataclass. `from_expr(expr)` classmethod
  parses the JSON expression. `find_prohibited_callees(callee_ids)` and
  `is_excluded(function_id)` are pure methods taking plain lists. No I/O.
- `src/call_graph/storage.py` — `get_nodes_missing_docstring(project_id, exclude_names)`
  and `get_all_caller_ids(project_id)` added. The previously-inline SQL queries now live
  in the right owner.
- `src/contracts/manager.py` — `_check_structural` uses `ContractRule.from_expr()`,
  `db.get_nodes_missing_docstring()`, `db.get_all_caller_ids()`. `_check_structural_for_function`
  uses `ContractRule` and `db.get_callees()` — the canonical traversal that already existed.

### Why `ContractRule` is a dataclass not a class hierarchy

There are three rule types (SEMANTIC, BOUNDARY, PRESENCE) but the structural check logic
is the same regardless. A single dataclass with `needs_metadata_check()` and
`needs_call_graph_check()` predicates handles all three without polymorphism. If rule
types diverge significantly in the future, a class hierarchy is easy to add later.
Don't add abstraction for hypothetical futures.

### The "one adapter" rule

The report noted this was a "one adapter" situation: there was only one caller of
`_check_structural_for_function`, so it was a hypothetical seam, not a real one. We
did it anyway because the SQL duplication was real (two places making the same
edge-table query) and `ContractRule` makes the logic testable without any database
setup. The seam became real the moment we had a pure object with unit-testable methods.

---

## Change 4 — `Services` typed dataclass in `server.py`

### What was wrong

`_services: dict[str, Any]` and `_get_services() -> dict[str, Any]` meant every handler
accessed services by string key: `svcs["db"]`, `svcs["embeddings"]`, etc. This has three
costs:

1. **No type safety.** A typo like `svcs["embedings"]` raises `KeyError` at runtime, not
   at the linter.
2. **Hidden dependencies.** Reading a handler doesn't tell you which services it uses —
   it calls `_get_services()` and could use anything.
3. **Dict truthiness.** The double-checked locking used `if _services:` (falsy when
   empty), which is subtly wrong if the dict is non-empty but uninitialized.

### What changed

- `Services` dataclass added to server.py with typed fields for each service.
- `_services: Services | None = None` — explicit `None` sentinel replaces the empty-dict
  sentinel.
- `_get_services() -> Services` — returns the typed container; uses `is not None` check
  for the locking guard.
- `global _services` added — necessary because we now assign to the variable, not mutate
  a mutable container.
- All 28 `svcs["key"]` accesses in `server.py` and 4 in `web/routes.py` replaced with
  attribute access (`svcs.db`, `svcs.embeddings`, etc.).
- Lifespan cleanup uses `if _services is not None:` instead of `if _services.get("db"):`.

### Why not full dependency injection at handler level

FastMCP doesn't support FastAPI-style `Depends()` injection on tool handlers. The
service locator pattern (`_get_services()` called in every handler) remains, but the
container is now typed. The dependency of each handler is still implicit, not
declared — but at least accessing the wrong key is now a type error, not a runtime
`KeyError`.

Full DI would require either wrapping every handler in a factory function (mechanical
but verbose) or waiting for FastMCP to add native support. The typed dataclass is the
right level of improvement given the framework constraint.

### Why `global _services` is needed now

The old code used `_services.update(...)` — mutating a dict in place. No `global`
statement needed because we weren't reassigning the variable. The new code assigns
`_services = Services(...)` — this creates a new object. Without `global _services`,
Python treats the assignment as a local variable and the module-level `_services` stays
`None`. This is a subtle Python scoping rule: mutation doesn't need `global`, assignment
does.

---

## Change 5 — `EmbeddingPipeline.model` (public property)

### What was wrong

`indexer.py:148` accessed `self._pipeline._model` — a private property on a class in a
different module. The underscore convention in Python signals "implementation detail of
this class." Crossing a module boundary to read it is a minor violation.

### What changed

`_model` renamed to `model` in `EmbeddingPipeline`. It was already a property delegating
to `self._store._model`. Making it public acknowledges that the configured embedding
model name is part of the pipeline's interface — callers have a legitimate reason to
know it (for log messages, for reporting).

---

## What was deliberately not changed

### Indexer non-atomic orchestration (Candidate 4 from the report)

`Indexer.index_project()` commits call graph writes before embedding writes. If embedding
fails, the call graph reflects new nodes but the vector store is stale.

This sounds alarming, but it's self-healing: the hash-diff mechanism in `index_project`
detects stale embeddings on the next run by comparing `body_hash` values. On the next
call, the changed functions are re-embedded. No data is lost; at worst, semantic search
quality degrades until the next index.

The full Parse/Reconcile/Commit decomposition described in the report would add a named
`IndexDelta` type and three named phases. This has real value for testability (the
reconcile logic is the most nuanced part), but the risk/reward is different from the
other candidates: it would touch the Indexer's main path and requires designing the
`IndexDelta` contract carefully.

**Decision:** leave it for a dedicated session when test coverage for the indexer becomes
a priority. Record this as an open design question, not a bug.

### `web/routes.py` `db._db.execute()` breach

`api_status` in `web/routes.py` uses `db._db.execute(...)` to count nodes and edges
directly, bypassing CallGraphDB. This is the same pattern we fixed in EmbeddingStore —
a cross-boundary raw SQL access to a table owned by another class.

Not fixed here because it requires adding `count_nodes()` and `count_edges()` methods
to CallGraphDB, which is a clean but non-trivial change. Noting it so it doesn't get
missed.

---

## Summary of files changed

| File | Change |
|---|---|
| `src/call_graph/models.py` | NEW — `GraphData`, `ArchitectureSnapshot` dataclasses |
| `src/analysis.py` | NEW — `ArchitectureAnalyzer` with 6 heuristic methods |
| `src/contracts/rule.py` | NEW — `ContractRule` pure dataclass |
| `src/embeddings/pipeline.py` | NEW — `EmbeddingPipeline` orchestrator |
| `src/call_graph/storage.py` | +8 methods: `fetch_graph_data`, `update_node_embedding_meta`, `commit`, `get_nodes_needing_enrichment`, `count_nodes_by_model`, `get_nodes_missing_docstring`, `get_all_caller_ids`, `get_project_home_data` thinned to 5 lines |
| `src/embeddings/embedder.py` | Removed `upsert_chunks`, `enrich_summaries`; added `upsert_vector` |
| `src/contracts/manager.py` | Removed `_db._db` breaches; uses `ContractRule` and proper DB methods |
| `src/indexer.py` | Receives `EmbeddingPipeline`; `self._embeddings` → `self._pipeline` |
| `src/server.py` | `Services` dataclass; all `svcs["key"]` → `svcs.key`; typed `_get_services()` |
| `src/web/routes.py` | `svcs["key"]` → `svcs.key` |

---

## The one principle underlying all five changes

**Complexity should be in the right place.** Not less complexity — the same complexity,
in a module named for what it actually does, accessible through an interface that
documents what it requires and promises.

`get_project_home_data` wasn't broken. It was in the wrong file. `upsert_chunks` wasn't
wrong. Its routing decision was buried where no one would look for it. `_check_structural`
wasn't buggy. Its rule logic was untestable because it was entangled with I/O.

The "deletion test" is the fastest way to see this: delete the module. If complexity
vanishes (a pass-through), the module wasn't earning its keep. If complexity reappears
in one place under a better name, that's a deepening — the module was earning its keep
all along, just in the wrong neighborhood.
