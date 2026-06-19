# Phronosis Roadmap

> **Vision:** Phronosis becomes the organizational substrate for a software development organization that runs without a standing engineering team. Humans define goals, constraints, and priorities. Agents handle implementation. Phronosis is the shared nervous system — memory, coordination, governance, and project management in one queryable layer.

---

## Pre-launch gate — Internal Documentation & Feature Audit

Before the public website (Phase 17): audit every feature end-to-end and build an internal reference site.

**Produces:**
- A verdict on every MCP tool, HTTP endpoint, web UI panel, and hook: ✅ ship | 🔧 fix | 🚫 remove
- An internal docs site (not public): full tool reference with live examples, architecture overview, ops runbook, known limitations, teaching series compiled, internal decision index
- A hard gate: nothing ships publicly that exists in the code but doesn't work

Content that passes review moves to `docs.phronosis.dev`. Limitations, internal architecture rationale, and ops details stay internal.

See Notion Phase 16 for full task breakdown.

---

## Active — implement next

### Guidance Layer — Context-Aware Tool Responses for Agent Code Generation

**The problem it solves:** Multi-agent systems fail when agents work from isolated context windows, make conflicting assumptions, and produce structurally inconsistent code that requires human reconciliation. The architectural knowledge needed to write correct code already exists in the Phronosis index — it just isn't surfaced in a form agents can act on. This feature makes tool responses prescriptive enough that any agent using Phronosis naturally produces well-structured, pattern-conformant code on the first attempt.

**The mechanism:** Not a separate pipeline. Not pre-computed artifacts. A signal detection layer that runs over the data already fetched by each tool call and appends a `_guidance` field to the response. The architectural understanding builds organically through use — first agent to query an area computes the signals and caches them; every subsequent agent retrieves them instantly.

---

#### Phase 1 — Guidance middleware + `query_similar_functions` (Week 1)

**New file: `src/guidance.py`**

Signal detection rules, all computed from existing index data — no LLM calls, no extra DB round-trips:

- **Concentration signal:** >75% of results in same module → surface module pattern, naming convention, suggest `get_decision_history(module_anchor)`
- **Chokepoint signal:** >50% of results share a common callee → surface the chokepoint, its caller count, suggest `get_decision_history(chokepoint)`
- **Decision gap signal:** results touch risk-surface functions with no logged decisions → "No decisions logged for this high-risk area — design carefully, log your decision after"
- **Contract signal:** results touch a module with active contracts → include contract summary inline, don't make agent ask separately
- **Performance signal:** any result has an active performance finding → warn agent not to replicate the pattern
- **Async distribution signal:** results are uniformly async or mixed → surface the convention and flag exceptions
- **Naming convention signal:** infer verb_noun or get_X_by_Y pattern from function name corpus in the module → include in response

Response shape (backward compatible — agents that don't read `_guidance` ignore it):
```json
{
  "results": [...],
  "_guidance": {
    "pattern_signal": "8/8 results in storage.py — strong module pattern",
    "confidence": 0.91,
    "active_constraints": [
      "All DB queries route through _DB.execute (43/43 functions in this module)"
    ],
    "suggested_follow_ups": [
      {
        "tool": "get_decision_history",
        "args": {"function_name": "CallGraphDB"},
        "reason": "Understand why DB access is centralized here before adding to it"
      }
    ]
  }
}
```

**Schema change:**
```sql
ALTER TABLE nodes ADD COLUMN architecture_context TEXT NOT NULL DEFAULT '';
```

Guidance computation result is stored back to the function node. Subsequent calls retrieve cached context instead of recomputing. Invalidated when `body_hash` changes on `index_changes`.

**Integration point:** `query_similar_functions` first — this is the highest-leverage tool because agents call it before generating. Runs the guidance computation concurrently with the main query via `asyncio.gather`. Target: <20ms added latency.

---

#### Phase 2 — Round out remaining tools (Week 2)

Integrate guidance into:

- **`get_function_context`** — include impact radius summary inline automatically (saves agents a separate call on high-impact functions)
- **`get_callers` / `get_callees`** — add module pattern if results cluster; surface `is_external` blind spots proactively
- **`get_decision_history`** — when empty, don't return `[]` silently; return "no decisions logged — adjacent decisions exist in [nearby functions], consider logging yours after the edit"
- **`check_performance`** — add structural cause mapping (N+1 → "missing repository/batch layer", sequential_awaits → "missing concurrency abstraction")

**New table (pure SQL aggregation, no LLM, regenerated on `index_changes`):**
```sql
CREATE TABLE module_patterns (
    project_id        TEXT NOT NULL,
    module            TEXT NOT NULL,
    naming_regex      TEXT,
    async_ratio       REAL,
    primary_chokepoints TEXT,  -- JSON: [{id, caller_count}]
    computed_at       TEXT NOT NULL,
    PRIMARY KEY (project_id, module)
);
```

---

#### Phase 3 — `validate_proposed_code` (Week 3)

New MCP tool. Agent calls this before writing to disk — the "A" in the A+C approach.

```
validate_proposed_code(code, target_file, project_id)
```

What it does — no LLM required:
1. Parse `code` with existing `TreeSitterParser` (in-memory, not indexed)
2. Extract: function names, direct callees, is_async, module path inferred from `target_file`
3. Compare against `architecture_context` cached for that module
4. Check against active contracts
5. Run performance pattern detection against the snippet

Returns:
```json
{
  "conformance_score": 0.73,
  "conforming": ["Names follow verb_noun convention", "Correctly scoped to project_id"],
  "deviations": [
    {
      "severity": "high",
      "issue": "Direct asyncpg.connect() — 43/43 existing DB functions use _DB.execute",
      "suggestion": "Use `await self._db.execute(query, (project_id,))`",
      "example": "storage.CallGraphDB.get_all_nodes (line 462)"
    }
  ],
  "contract_violations": [],
  "performance_risks": ["Loop calling DB function — potential N+1"]
}
```

Conformance <0.7 → agent has enough information to self-correct before writing.

---

#### What this does NOT require

- No separate LLM pipeline
- No new background jobs or scheduled tasks
- No breaking changes to any existing tool
- No changes to how agents call tools today

The premium `analyze_architecture` LLM pipeline (richer pattern descriptions, cross-module synthesis) can layer on top later as an upgrade to `architecture_context` quality. The guidance layer works without it — structural signals only. Both are useful; neither requires the other.

---

#### Why this changes the competitive position

Every competitor (CodeGraph, GitNexus, Greptile) surfaces structural data. None of them tell agents what to do with it. The guidance layer is the difference between "here are 8 similar functions" and "8/8 of those functions follow this pattern, here's the constraint you're working under, here's what to ask next." That second response produces conformant code. The first produces a guess.

This directly addresses the core failure mode of multi-agent systems identified in product research: agents making conflicting assumptions because they share no institutional memory. With the guidance layer active, agents working in established areas of the codebase converge on established patterns automatically — not because they were told to, but because the tool response makes the correct path visible.

---

---

## Back burner — design needed before building

### Multi-Language Support
Phronosis currently parses 8 languages precisely (Python, TypeScript/JS, Rust, Go, Java, C++, C#, Ruby). Competitors CodeGraph (38 languages) and GitNexus (multi-language) use language breadth as a positioning lever. Three-phase approach:

**Phase 1 — Generic fallback parser.** One `_parse_generic()` that identifies function-like constructs from any tree-sitter grammar using node-type heuristics (`"function"`, `"method"`, `"declaration"` in node type name). Rough accuracy — misses class membership, async, return types — but gives blast-radius coverage for any grammar. Reports `structural_layer: "tree-sitter-generic"` so callers know the quality level. ~150 lines, one PR, immediate 40+ language claim.

**Phase 2 — Precision parsers for priority gaps.** Swift, Kotlin, PHP in that order. Each follows the existing Rust parser as a template (~120 lines, ~2–4 hours each). These cover the mobile and legacy web markets that are currently unserved.

**Phase 3 — SCIP-first for typed languages.** Expand SCIP indexer support to Go (`scip-go`), Java (`scip-java`), Rust (`rust-analyzer`), C# (`scip-dotnet`). Type-resolved call graphs close the accuracy gap that tree-sitter-only competitors cannot close. Current architecture already supports SCIP as an enhancement; make it primary when available.

**Dependency:** Phase 1 can ship independently. Phase 3 requires SCIP indexers to be installable in the target environment (add to `setup.sh`).

---

### IDE Extension *(TODO — design settled, not yet started)*
Surface Phronosis context inline — impact radius on hover, performance findings as diagnostics, decision history in a sidebar — without leaving the editor.

**VS Code first** (larger developer market, simpler extension API). JetBrains as a follow-on once VS Code is stable.

Minimum viable scope (1–2 weeks):
- File save → `index_changes` automatically (removes the manual step from the workflow; highest single-feature value)
- Status bar: index freshness, risk detection mode
- Command palette: "Show Impact Radius", "Check Performance", "Show Decision History" for function at cursor
- Inline diagnostics: `check_performance` findings rendered as VS Code `Diagnostic` objects with severity mapping

Differentiator vs CodeGraph/GitNexus extensions: decision history inline on hover — not just "42 callers" but the *why* logged when the function was designed.

**Key design decision before building:** whether the extension talks to the MCP HTTP transport (already exists) or a dedicated REST API. MCP is designed for AI agents, not human-facing tooling — the response shape may not be ideal for rendering in a hover tooltip. A thin REST layer over the same DB queries is probably cleaner for the extension.

**Dependency:** Multi-language support (Phase 1) makes the extension useful to non-Python teams. Can ship without it, but breadth matters for extension adoption.

---

### Risk-Gated Deployment
Replace binary CI pass/fail with a semantic risk score for each deployment.

Inputs: impact radius of changed functions, semantic similarity to prior incidents (from decision history), invariant contract violations, intent-structure gap score. Below a threshold: auto-deploy. Above: surface to human with a plain-language explanation of exactly what's risky and why.

Human attention becomes a finite resource allocated by evidence, not spent uniformly on every deploy. Requires structural change to the deployment pipeline and a concept of "incidents" in the data model. **Dependency: Invariant Contracts.**

---

### Multi-Agent Coordination
Phronosis as shared semantic state for multiple concurrent agents on the same codebase.

When agent A changes an API contract, it doesn't send a message to agent B — it updates Phronosis. Agent B, before implementing anything that touches that contract, queries Phronosis and discovers the change. Coordination through semantic state, not message passing. Requires multi-agent runtime infrastructure and a subscription/notification model on function-level changes.

---

## Ideas to explore — not yet prioritized

### Temporal Semantic Graph
Track the history of semantic meanings over time. When a function's embedding crosses a distance threshold from its prior state, that's a semantic drift event — the function's *purpose* changed. Semantically stable functions are safe anchor points; semantically volatile ones are risk vectors. Pairs with runtime usage metrics.

### Runtime Usage + Observability Feedback Loop
Instrument production or test runs to capture behavioral data: which functions actually execute, how often, in what sequences. Pair high-frequency functions with higher stability requirements and development priority. Close the incident-to-fix loop: anomalous runtime path → semantic search → prior incident decision history → fix pattern → deploy.

### Intent-Structure Gap Detector
Compare the semantic embedding of a function's decision history (what it was designed to do) against the embedding of its implementation (what it actually does). High divergence = implementation drifted from stated intent. Surface as a standing query: "show all functions where the code no longer matches the design."

### Cross-Language Isomorphism
Detect functions doing the same thing across Python and TypeScript projects. Embeddings are language-agnostic, so this works today — the missing piece is a standing isomorphism map that monitors when semantically paired functions diverge.

### Semantic Test Coverage
Measure whether tests cover the *behaviors* functions implement, not just the lines. Embed test functions alongside production functions. High line coverage + large semantic distance between test and function = undertested behavior, not just uncovered lines.

---

## Implemented

| Feature | Date | Notes |
|---|---|---|
| Three-layer architecture (call graph, embeddings, decisions) | initial | |
| Function-level hash diffing for incremental re-index | 2026-06-08 | Only changed functions re-embed |
| Multi-project support with project_id namespace | 2026-06-08 | Schema migration at startup |
| Per-project vec0 tables | 2026-06-09 | True isolation, no cross-project KNN noise |
| Parallel LLM summarization | 2026-06-09 | asyncio.gather + Semaphore(10) |
| Similarity score normalization | 2026-06-09 | L2 → [0,1] match percentage |
| Web UI project selector + search panel | 2026-06-09 | |
| `/phronosis-import` slash command | 2026-06-08 | Three-step onboarding in one command |
| ~~Project Management Interface~~ | — | Skipped — markdown files + function ID references serve the same purpose without a dedicated tracker |
| Invariant Contracts | 2026-06-09 | LLM-generated violation/compliance examples, structural + semantic enforcement, MCP tools, web UI, post-commit hook. Destructive ops (delete/update) web-UI-only to prevent agent bypass. |
| Project Home (`get_project_home`) | 2026-06-09 | Single MCP call returns subsystems, wiring, chokepoints, risk surface, health. Replaces file reads for architectural understanding. |
| `setup_phronosis_client` one-call onboarding | 2026-06-09 | Generates setup script: hooks, settings.json, CLAUDE.md, memory files, git hook. |
| PreToolUse hooks (Bash/Read/Edit) | 2026-06-09 | Risk-signal check on Edit; Phronosis nudge on grep/Read. |
| PostToolUse/Edit hook | 2026-06-09 | Auto-indexes edited file in background; warns when client_setup.py template sources are modified. |
| Agent Improvement Filing (`file_improvement` / `list_improvements` / `resolve_improvement`) | 2026-06-09 | Cross-session agent crosstalk: agents file structured bug/enhancement reports mid-session; a later session reads open items and implements them. SQLite-backed, no external dependency. |
