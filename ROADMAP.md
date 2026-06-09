# ACIP Roadmap

> **Vision:** ACIP becomes the organizational substrate for a software development organization that runs without a standing engineering team. Humans define goals, constraints, and priorities. Agents handle implementation. ACIP is the shared nervous system — memory, coordination, governance, and project management in one queryable layer.

---

## Active — implement next

*No active items. Select from back burner or ideas below.*

---

## Back burner — design needed before building

### Risk-Gated Deployment
Replace binary CI pass/fail with a semantic risk score for each deployment.

Inputs: impact radius of changed functions, semantic similarity to prior incidents (from decision history), invariant contract violations, intent-structure gap score. Below a threshold: auto-deploy. Above: surface to human with a plain-language explanation of exactly what's risky and why.

Human attention becomes a finite resource allocated by evidence, not spent uniformly on every deploy. Requires structural change to the deployment pipeline and a concept of "incidents" in the data model. **Dependency: Invariant Contracts.**

---

### Multi-Agent Coordination
ACIP as shared semantic state for multiple concurrent agents on the same codebase.

When agent A changes an API contract, it doesn't send a message to agent B — it updates ACIP. Agent B, before implementing anything that touches that contract, queries ACIP and discovers the change. Coordination through semantic state, not message passing. Requires multi-agent runtime infrastructure and a subscription/notification model on function-level changes.

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
| `/acip-import` slash command | 2026-06-08 | Three-step onboarding in one command |
| ~~Project Management Interface~~ | — | Skipped — markdown files + function ID references serve the same purpose without a dedicated tracker |
| Invariant Contracts | 2026-06-09 | LLM-generated violation/compliance examples, structural + semantic enforcement, MCP tools, web UI, post-commit hook. Destructive ops (delete/update) web-UI-only to prevent agent bypass. |
| Project Home (`get_project_home`) | 2026-06-09 | Single MCP call returns subsystems, wiring, chokepoints, risk surface, health. Replaces file reads for architectural understanding. |
| `setup_acip_client` one-call onboarding | 2026-06-09 | Generates setup script: hooks, settings.json, CLAUDE.md, memory files, git hook. |
| PreToolUse hooks (Bash/Read/Edit) | 2026-06-09 | Risk-signal check on Edit; ACIP nudge on grep/Read. |
| PostToolUse/Edit hook | 2026-06-09 | Auto-indexes edited file in background; warns when client_setup.py template sources are modified. |
