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

### ✅ Phase 14 — Demo Repos (complete 2026-06-19)

All 12 repos indexed, verified, and documented.

- [x] Run 5 representative queries per repo and verify relevance — similarity 0.70–0.83 across requests, pytest, django; semantic search confirmed working
- [x] Check `get_project_home()` for each repo — per-repo 2–3 sentence briefs written in `docs/demo-repos.md`
- [x] Verify write tools return 403 for demo projects for non-admin users — enforced by `check_project_access` in storage.py; covered by `test_demo_project_write_raises_403` in tests/test_auth.py
- [x] Remove `COPY scripts/ ./scripts/` from Dockerfile — done

---

### Phase 16 — Internal Documentation & Feature Audit

Prerequisite for Phase 17 (public launch). Enumerate and verify every feature surface:

- [ ] **MCP tools** — run each tool against Phronosis itself; verify response is correct, not just non-erroring
- [ ] **HTTP endpoints** — test every `/api/*` route end-to-end
- [ ] **Web UI panels** — search, contracts, project home, improvements — verify each renders and all interactions work
- [ ] **Claude Code hooks** — PreToolUse/PostToolUse: verify each fires in a live session with the right output
- [ ] **Post-commit hook** — verify it fires, re-indexes, and logs a decision on a real commit
- [ ] **CLI scripts** — `create_user.py`, `index_demo_repos.py`, `backfill_decisions.py` — verify each runs clean with current schema
- [ ] **Verdict per feature:** ✅ ship | 🔧 fix before Phase 17 | 🚫 remove

**Hard gate:** Nothing ships to Phase 17 that exists in the code but doesn't work end-to-end.

**Internal docs site produces:**
- Full MCP tool reference with one working example per tool against a demo repo
- Architecture overview — the four-layer model and design rationale
- Teaching series compiled (Lessons 1–5 + remaining tools: `check_performance`, `check_contracts`, `get_branch_conflicts`, `enrich_summaries`)
- Ops runbook — deploy, re-index, add demo repo, rotate keys, diagnose broken index
- Known limitations — honest internal notes (false positive rates, scale ceilings, unsupported patterns)

See Notion Phase 16 for full task breakdown.

---

### Codebase Architecture Improvements (parallel, pre-launch)

From Notion — not blockers, but compound at scale:

- [ ] **`get_decision_history` returns empty for most functions** — `risk_detection_mode: "structural_heuristic_no_decisions"` in `get_project_home` is the symptom. Diagnose: is `log_decision()` never called, or does the post-commit hook skip `linked_function_ids`? Check decisions table directly. Fix the gap, then verify a real edit produces a non-empty `get_decision_history`.
- [ ] **Anchor summaries** — 25/26 Phronosis subsystems have empty `anchor_summary`. Write a 1-line docstring per subsystem anchor class so `get_project_home` tells agents what each subsystem *does*, not just its name.
- [ ] **Structured logging** — replace `print()` in `src/` with `logging.getLogger()`, add request ID per line
- [ ] **Config class** — single `Config` dataclass at startup, replace scattered `os.getenv()` calls
- [ ] **Typed errors** — `PhronosisError`, `NotFoundError`, `PermissionError`, `RateLimitError`
- [ ] **Queue depth limit per user** — Redis counter per `user_id`; free tier: 1 enrich/month, 3 index/month; pro: 10 enrich/month, unlimited index (Phase 12 partial — rate limiting by concurrent job exists, per-plan monthly cap does not)
- [ ] **Separate transport from logic** — extract core operations into `src/services/`; `server.py` becomes thin routing only
- [ ] **DNS** — `api.phronosis.dev` → ingress (blocked until TLS/cert-manager sorted for cloud move)

---

## Back burner — design needed before building

### IDE Extension *(design settled, not yet started)*
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
| Auth layer (Phase 10) | 2026-06-14 | `users`, `api_keys`, `project_access`, `demo_projects` tables. `AuthMiddleware` + `check_permission`. `POST /api/signup`, `GET /api/me`. `scripts/create_user.py`. |
| Postgres + pgvector (Phase 11) | 2026-06-14 | Replaced aiosqlite+sqlite-vec with asyncpg pool + pgvector. HNSW index on embeddings. `schema.sql` ported. Migration script `scripts/migrate_sqlite_to_postgres.py`. |
| Background worker queue (Phase 12) | 2026-06-14 | Redis + RQ. `index_project` and `enrich_summaries` enqueued. `GET /api/jobs/{job_id}`. Per-user concurrent job limit (429 on duplicate). |
| Kubernetes deployment (Phase 13) | 2026-06-14 | K3s on Unraid (TheHive). Manifests: namespace, api-deployment (2 replicas), worker, postgres StatefulSet, redis, secrets, ingress, HPA-api. CI/CD via GitHub Actions → GHCR → kubectl apply over Tailscale. |
| Demo repos indexed (Phase 14 — partial) | 2026-06-16 | 12 SWE-bench repos indexed and enriched. Actual cost: $6.85 ($0.76/repo). Remaining: query verification, write-403 check, Dockerfile cleanup. |
| `return_type`, `is_async`, `parameter_names`, `enclosing_class`, `start_line`/`end_line` on nodes | 2026-06-18 | Structured metadata extracted by all parsers and stored on `nodes`. |
| `ArchitectureAnalyzer` extracted from `CallGraphDB` | 2026-06-18 | Pure sync class in `src/analysis.py`. `GraphData`/`ArchitectureSnapshot` dataclasses in `call_graph/models.py`. |
| `EmbeddingPipeline` extracted from `EmbeddingStore` | 2026-06-18 | Routing strategy (small vs large model) moved to `src/embeddings/pipeline.py`. |
| `ContractRule` extracted from `ContractManager` | 2026-06-18 | Pure dataclass in `src/contracts/rule.py`. Rule logic unit-testable without DB. |
| `Services` typed dataclass in `server.py` | 2026-06-18 | Replaced `dict[str, Any]` with typed container. All `svcs["key"]` → `svcs.key`. |
| Guidance Layer — all 3 phases | 2026-06-19 | `src/guidance.py` (7 signals on 5 tools), `src/validate.py` (`validate_proposed_code`), `src/architecture_preflight.py` (`preflight_architecture`). Backward-compatible `_guidance` field. |
| `ArchitectureService` extracted from `CallGraphDB` | 2026-06-19 | Cache + analysis orchestration in `src/architecture_service.py`. `CallGraphDB` is now pure SQL. |
| Multi-Language Support — all 3 phases | 2026-06-19 | **Precision (11 languages):** Python, TS/JS, Rust, Go, Java, C++, C#, Ruby, Swift, Kotlin, PHP. **Generic fallback (15 languages):** Bash, Lua, Scala, C, OCaml, Elixir, Haskell, Zig, Groovy, Perl, Common Lisp, Fortran, Solidity, Julia, Odin, MATLAB. **SCIP Phase 3:** Go, Java, Rust, C# added to cmd_map. 55 extensions total. `structural_layer` field on all nodes. |
| Generic-layer degradation fixes | 2026-06-19 | `_async_signal` now uses `is_async` field (not Python-only regex). `_check_async` skips when all existing nodes are `structural_layer='generic'`. `query_similar` returns `is_async`+`structural_layer`. |
