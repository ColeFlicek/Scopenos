---
name: acip-workflow
description: "ACIP code intelligence for indexed codebases. Mandatory workflow: call get_project_home(project_id) FIRST every session before any source file read. Tier order: (1) get_project_home — architecture snapshot; (2) query_similar_functions / get_impact_radius / get_decision_history — function context; (3) Read — only for the exact lines you are about to modify. Never read files to understand structure; use ACIP tools instead. Use on any ACIP-indexed project."
---

# ACIP Workflow

## Session Start — Three-Tier Retrieval Ladder

Run these in order at the start of every session. Do not skip Tier 1.

**Tier 1 — one call, full architectural picture (~500 tokens):**
```
get_project_home("project_id")
```
Returns: subsystems, wiring diagram, chokepoints, entry points, risk surface,
health (top knowledge gaps, contract violations, churn hotspots), recent
decisions, and a `since_last_session` diff showing what changed since the last
call. This single call replaces reading any files for architectural understanding.

**Tier 2 — targeted queries for the specific task:**
```
query_similar_functions("<feature>", top_k=8)   # find existing patterns
get_impact_radius("<function>", depth=2)         # what breaks if this changes?
get_decision_history("<function>")               # why was this designed this way?
get_callers("<function>")                        # who calls this?
get_callees("<function>")                        # what does this call?
query_decisions("<topic>")                       # prior decisions on a topic
```

**Tier 3 — file reads, precision only:**
```
Read(file, specific_lines)   # only after knowing the exact function to modify
```

---

## Pre-Edit Gate

Before every Edit on an existing function, run all three:

1. `get_impact_radius(fn, depth=2)` — what breaks if signature or behavior changes?
2. `get_decision_history(fn)` — why was this designed this way? what was rejected?
3. `query_similar_functions(what_you_are_about_to_write)` — existing pattern in this codebase?

Check 3 is the structural consistency check — inconsistent patterns inside a
codebase are a class of bugs. In multi-agent contexts, check 2 also reveals
whether a concurrent agent modified this function since your last session.

---

## Tool Reference

| Need | Tool |
|---|---|
| Architecture / what exists | `query_similar_functions(concept, top_k=10)` |
| Who calls this? | `get_callers(function_name)` |
| What does this call? | `get_callees(function_name)` |
| What breaks if I change X? | `get_impact_radius(function_name, depth=2)` |
| Why was this designed this way? | `get_decision_history(function_name)` |
| Prior decisions on a topic | `query_decisions(query_text)` |
| Full project snapshot | `get_project_home(project_id)` |
| What changed since last session | `get_project_home` → `since_last_session` field |

Fall back to grep or Read only if a query returns empty or the project is not indexed.

---

## After Edits

```
index_changes(["modified_file.py"], {"modified_file.py": "<full content>"})
```

Keeps the index fresh within the session so subsequent queries reflect your changes.

---

## Session End

Log significant design or implementation decisions made this session:

```
log_decision(
    type="Architectural | Design | Implementation | Patch",
    description="what was decided and why",
    rejected_alternatives="what was considered and not chosen",
    trigger="ticket ID, CVE, UX finding, or reason",
    linked_function_ids=["module.ClassName.method"],
    project_id="project_id"
)
```

The post-commit git hook handles commit-level decisions automatically.
`log_decision` is for in-session choices that don't map to a single commit.

---

## Multi-Agent Context

When multiple agents work on the same codebase concurrently:

- `get_decision_history(fn)` before ANY edit — a concurrent agent may have just modified it
- `get_project_home` → `since_last_session` shows what changed between sessions
- `log_decision()` immediately after significant choices — the next agent reads it before touching the same code
- Contracts apply to all agents — `check_contracts(project_id)` if you're adding new functions to an enforced project
