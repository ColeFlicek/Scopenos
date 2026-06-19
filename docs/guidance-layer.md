# Guidance Layer — Complete Signal Reference

What every check means, how it's calculated, what the thresholds are, and what's missing.

---

## Overview

The Guidance Layer intercepts five MCP tool responses and enriches them with a `_guidance`
field computed from existing index data. No LLM calls. No separate pipeline. All signals
fire in <20ms by running pure logic on the result set or by issuing 1–3 targeted DB queries
in parallel.

The goal: agents using Phronosis tools produce architecturally conformant code on the first
attempt — not because they were told to, but because the tool response makes the correct path
visible.

**Covered tools:**

| Tool | Signal category |
|---|---|
| `query_similar_functions` | 7 discovery signals |
| `get_callers` | 3 caller-context signals |
| `get_callees` | 3 callee-surface signals |
| `get_decision_history` | 1 empty-case signal |
| `check_performance` | 1 structural-cause mapping |
| `validate_proposed_code` | 4 conformance checks |

**19 signals total. 8 pure (zero DB). 11 DB-backed.**

---

## Part 1 — Current Signals

### `query_similar_functions` — 7 signals

These fire on every call that includes a `project_id`. The result set is the list of top-k
functions returned by KNN vector search.

---

#### 1. Concentration Signal
**Location:** `src/guidance.py → _concentration_signal()`

**What it means:** The agent's query has a strong affinity for one module. That module is
probably where the new code belongs or what the new code must interact with.

**How it's calculated:**
```
counts = Counter(r["module"] for r in results)
dominant_module, cnt = counts.most_common(1)[0]
ratio = cnt / len(results)
fires when ratio >= 0.75
```

**Output:**
```
pattern_signal: "7/8 results in `src.call_graph.storage` — strong module concentration"
confidence: 0.875
```

**Why 0.75?** Below that threshold the signal is noise. A 6/8 split is meaningful; a 5/8
split in an 8-result set is essentially "majority module" not "module concentration."

**False positive rate:** Low. The KNN search is what caused the concentration; if the query
had broad intent the results would be spread.

---

#### 2. Chokepoint Signal
**Location:** `src/guidance.py → _chokepoint_follow_ups()`

**What it means:** A function in the result set has many callers. Editing it propagates
changes widely. The agent should understand the impact radius before modifying it.

**How it's calculated:**
```sql
SELECT callee_id, COUNT(DISTINCT caller_id) AS cnt
FROM edges
WHERE project_id = $1 AND callee_id = ANY($2)
GROUP BY callee_id
```
```
fires when cnt >= CHOKEPOINT_THRESHOLD (= 15)
```

**Output:**
```json
{
  "tool": "get_impact_radius",
  "args": {"function_name": "_DB.execute", "depth": 2},
  "reason": "Chokepoint with 90 callers — changes propagate widely"
}
```

**Why 15?** The Phronosis codebase has `_DB.execute` at 90 callers and a typical utility at
3–5. The threshold of 15 sits cleanly above "commonly used" and below "genuinely structural."
Projects with fewer functions may need a lower threshold.

**Cost:** 1 batch query on `edges` (indexed on `callee_id`). Results ordered by count so
the highest-risk chokepoint appears first.

---

#### 3. Decision Gap Signal
**Location:** `src/guidance.py → _decision_gap_follow_ups()`

**What it means:** A high-caller function in the results has no logged decisions. This is
the most dangerous state in multi-agent development: many agents depend on it, but no agent
knows *why* it was designed the way it was. A future agent will guess wrong.

**How it's calculated:**
```sql
SELECT DISTINCT function_id FROM decision_functions WHERE function_id = ANY($1)
```
```
fires when:
  caller_count >= CHOKEPOINT_THRESHOLD (15)
  AND function_id NOT IN functions_with_decisions
```

**Output:**
```json
{
  "tool": "get_decision_history",
  "args": {"function_name": "_DB.execute"},
  "reason": "High-caller function (90 callers) with no logged decisions"
}
```

**Why only chokepoints?** Flagging every undocumented function would be too noisy. The
chokepoint threshold creates a high-value target list — these are the functions where missing
context causes the most downstream harm.

**Cost:** 1 batch query on `decision_functions` (runs in the same `asyncio.gather` as the
chokepoint query, adding ~0ms).

---

#### 4. Contract Signal
**Location:** `src/guidance.py → _contract_constraints()`

**What it means:** An active contract covers one or more functions in the result set.
The agent's new code must satisfy this constraint.

**How it's calculated:**
```
active_contracts = [c for c in list_contracts(project_id) if c["status"] == "active"]
for each contract:
  - empty function_ids → project-wide, always fires
  - "module.*" → prefix match against result IDs
  - exact ID → exact match
```

**Output:**
```
active_constraints: ["Contract: All DB queries must route through _DB.execute"]
```

**Matching logic:** Three tiers — project-wide (empty `function_ids`), wildcard glob
(`src.storage.*`), or exact ID. This mirrors `get_contracts_for_function`'s matching rules
exactly so the behavior is consistent.

**Cost:** 1 query on `contracts` (already fetched in the same `asyncio.gather`). No
extra cost when there are no active contracts.

---

#### 5. Performance Signal
**Location:** `src/guidance.py → _performance_suggestion()`

**What it means:** The result set contains async functions in a module known to have
I/O-intensive patterns. Running `check_performance` before writing new code here is
especially valuable.

**How it's calculated:**
```python
_PERFORMANCE_SENSITIVE_MODULES = frozenset({
    "src.call_graph.storage",
    "src.embeddings.embedder",
    "src.embeddings.pipeline",
    "src.indexer",
    "src.decision_memory.memory",
})

fires when:
  any result has "async def" in signature
  AND any result's module in _PERFORMANCE_SENSITIVE_MODULES
```

**Output:**
```json
{
  "tool": "check_performance",
  "args": {"project_id": "Phronosis"},
  "reason": "Async functions in I/O-heavy module — sequential await or N+1 pattern may be present"
}
```

**Limitation:** This is a static module allowlist, not dynamic. It misses async functions
in other modules that also do I/O. See Part 2 for the improvement.

**Cost:** Zero — pure set intersection. No DB call.

---

#### 6. Async Distribution Signal
**Location:** `src/guidance.py → _async_signal()`

**What it means:** The module has a clear async-first or sync-first convention. This tells
the agent which style to use for new code in the same module.

**How it's calculated:**
```python
async_count = sum(1 for r in results if "async def" in r.get("signature", ""))
ratio = async_count / total

ratio >= 0.8  → "Module is async-first (N/M results are async functions)"
ratio <= 0.2  → "N/M async functions — module is mostly sync; async is the exception"
0.2 < ratio < 0.8 → "Mixed async/sync (N/M async) — verify which convention applies"
ratio == 0    → no signal (all-sync is the default, not notable)
```

**Cost:** Zero. The `signature` field is already in the query result set.

**Note on data source:** The `signature` field starts with `async def` for async Python
functions, so the check is a simple substring match. For other languages this would need
adjustment.

---

#### 7. Naming Convention Signal
**Location:** `src/guidance.py → _naming_signal()`

**What it means:** The module uses a dominant `verb_*` naming convention. New functions
should follow the same verb prefix.

**How it's calculated:**
```python
_VERB_RE = re.compile(r"^_*([a-z]+)_")

for each result:
  name = bare_name (strips "ClassName." prefix)
  match = _VERB_RE.match(name)
  if match: verb_counter[match.group(1)] += 1

dominant_verb, cnt = verb_counter.most_common(1)[0]
ratio = cnt / len(results)
fires when ratio >= 0.6
```

**Output:**
```
"Dominant naming pattern: `get_*` (6/8 functions)"
```

**Why 0.6?** Below 60% there's no clear pattern. At 60%+ an agent can confidently name
new functions by the same rule.

**Class method handling:** `CallGraphDB.get_callers` → strips `CallGraphDB.` → `get_callers`
→ verb `get`. This prevents the class name from polluting the verb extraction.

**Cost:** Zero — computed from the `name` field already in results.

---

### `get_callers` — 3 signals

These fire for every `get_callers` call, using the caller list already returned.

---

#### 8. Caller Concentration
**Location:** `src/guidance.py → compute_callers_guidance()`

**What it means:** Callers are concentrated in one module (internal utility) or spread
across many modules (widely used interface). This tells the agent how broadly a change
will be felt.

**How it's calculated:**
```python
module_counts = Counter(c["module"] for c in callers)
dominant, cnt = module_counts.most_common(1)[0]
ratio = cnt / len(callers)

ratio >= 0.75: concentrated — "N/M callers in `module`"
len(modules) >= 5: spread — "Callers span N modules — widely used function"
```

**Cost:** Zero — computed from the caller list.

---

#### 9. Caller Count / Chokepoint Warning
**Location:** `src/guidance.py → compute_callers_guidance()`

**What it means:** The function has ≥15 callers — it's a structural chokepoint. Changes
should come with `get_impact_radius` first.

**How it's calculated:**
```python
fires when len(callers) >= CHOKEPOINT_THRESHOLD (15)
```

**Output:** Adds `get_impact_radius` to `suggested_follow_ups`.

---

#### 10. Async Caller Context
**Location:** `src/guidance.py → compute_callers_guidance()`

**What it means:** If all callers are async, this function always runs in an async context
(even if it's sync itself). Relevant when deciding whether to make it async.

**How it's calculated:**
```python
async_count = sum(1 for c in callers if "async def" in c.get("signature", ""))

all callers async → "All callers are async — function runs exclusively in async context"
< 30% async → "N/M callers are async — mostly sync calling context"
```

---

### `get_callees` — 3 signals

---

#### 11. External Dependency Surface
**Location:** `src/guidance.py → compute_callees_guidance()`

**What it means:** The function calls external libraries directly. Every external callee
is a dependency surface — a seam where the codebase is coupled to something outside its
control.

**How it's calculated:**
```python
externals = [c for c in callees if c.get("is_external")]
fires when len(externals) > 0
```

**Output:**
```
"3 external callee(s) — direct dependency on: aiohttp, asyncpg, openai"
```

---

#### 12. Adapter Layer Suggestion
**Location:** `src/guidance.py → compute_callees_guidance()`

**What it means:** 3+ external callees with no shared wrapper suggests the function is
doing direct library access that should go through an adapter. The adapter creates a seam
for testing and future library swaps.

**How it's calculated:**
```python
fires when len(externals) >= 3
```

The threshold of 3 comes from the "two adapters" rule: one file calling a library isn't
scattered; three files calling it directly means no one owns the abstraction.

---

#### 13. Internal Callee Concentration
**Location:** `src/guidance.py → compute_callees_guidance()`

**What it means:** 75%+ of internal callees are in one module — strong coupling. Before
modifying this function, understand that module's design decisions.

**How it's calculated:**
```python
internal = [c for c in callees if not c.get("is_external")]
module_counts = Counter(c["module"] for c in internal)
dominant, cnt = module_counts.most_common(1)[0]
ratio = cnt / len(internal)
fires when ratio >= 0.75 AND len(internal) > 2
```

**Output:** Adds `get_decision_history` for the dominant callee module to `suggested_follow_ups`.

---

### `get_decision_history` — 1 signal

---

#### 14. Empty Decision Case
**Location:** `src/guidance.py → compute_decision_guidance()`

**What it means:** No decisions logged. For a recently written function this may be fine.
For a chokepoint or complex function, it means any agent editing it is flying blind.

**Current behavior (before Guidance Layer):** Returns `[]` — silent, no context.

**New behavior:** Returns:
```json
{
  "decisions": [],
  "_guidance": {
    "note": "No decisions logged for `_DB.execute`. Logging decisions documents architectural intent...",
    "suggested_follow_ups": [
      {"tool": "get_callers", "reason": "Understand scope..."},
      {"tool": "log_decision", "reason": "Record the design intent..."}
    ]
  }
}
```

The empty case now actively guides the agent toward the right next action instead of
silently returning nothing.

---

### `check_performance` — 1 signal

---

#### 15. Pattern → Structural Cause Mapping
**Location:** `src/guidance.py → compute_performance_guidance()` + `PATTERN_CAUSE`

**What it means:** A performance finding is a symptom. The structural cause is why it
keeps happening. Fixing the structure prevents recurrence; fixing the symptom doesn't.

**How it's calculated:**
```python
PATTERN_CAUSE = {
    "n_plus_one":             "Missing repository/batch layer — queries issued per item instead of in bulk",
    "external_call_in_loop":  "Missing adapter/concurrency abstraction — external latency serialized per iteration",
    "correlated_join_aggregate": "Query logic leaking into wrong layer — aggregation SQL needs a dedicated query module",
    "sequential_awaits":      "Missing concurrency abstraction — independent I/O runs sequentially",
    "quadratic_expansion":    "Missing complexity bound at interface — O(n²) behaviour crosses a module boundary",
}

for each unique pattern in active findings:
  append structural_cause entry with count, affected_files, and fix direction
```

**Output:**
```json
{
  "structural_causes": [
    {
      "pattern": "n_plus_one",
      "count": 3,
      "structural_cause": "Missing repository/batch layer...",
      "affected_files": ["src/indexer.py", "src/server.py"]
    }
  ]
}
```

**Suggested follow-up:** `get_impact_radius` for the highest-severity finding, capped at 3
to avoid overwhelming the agent.

---

### `validate_proposed_code` — 4 conformance checks

These run before the agent writes code to disk. The parser processes the proposed string
in-memory using `TreeSitterParser.parse_file()`.

---

#### 16. Naming Conformance
**Location:** `src/validate.py → _check_naming()`

**What it means:** The proposed function name doesn't follow the module's verb prefix
convention. Agents that name inconsistently create codebase entropy — future semantic
searches return misleading results because similar concepts have different names.

**How it's calculated:**
```python
existing_names = [n["name"] for n in existing]   # from get_nodes_by_file(target_file)
dominant_verb, ratio = _dominant_verb(existing_names)
# _dominant_verb fires when ratio >= 0.6

for each proposed function:
  if verb_prefix != dominant_verb → deviation (severity: medium)
```

**Score deduction:** −0.15 (medium)

**Example output:**
```
"`fetch_session` does not follow the module's `get_*` naming convention"
"Existing: get_all_nodes, get_callers, get_callees, get_nodes_by_file"
```

---

#### 17. Async Conformance
**Location:** `src/validate.py → _check_async()`

**What it means:** The proposed function's sync/async nature breaks the module's
established convention. In an async-first codebase, a sync function that blocks is a
latency bomb. In a sync codebase, an unnecessary `async def` creates overhead and
misleads callers.

**How it's calculated:**
```python
existing_async_ratio = sum(1 for n in existing if n.get("is_async")) / len(existing)

ratio > 0.7  → async-first module
  sync proposed → severity: HIGH (breaks async call chain)
  
ratio < 0.3  → sync-first module
  async proposed → severity: MEDIUM (unnecessary overhead)

0.3 ≤ ratio ≤ 0.7 → mixed → no signal (can't infer convention)
```

**Score deductions:** −0.25 (high, async violation in async-first module), −0.15 (medium)

---

#### 18. Sequential Awaits
**Location:** `src/validate.py → _check_sequential_awaits_in_proposed()`
Delegates to: `src/performance.py → _detect_sequential_awaits(body)`

**What it means:** Two or more `await` expressions in the proposed function could run
concurrently with `asyncio.gather()` but run sequentially. Each extra await adds full
round-trip latency.

**How it's calculated:**
```python
_AWAIT_ASSIGN_RE = re.compile(r"\b(\w+)\s*=\s*await\b")
_GATHER_RE = re.compile(r"\basyncio\.gather\b|\bcreate_task\b|\bTaskGroup\b")

skips if body already uses gather/create_task/TaskGroup
skips if consecutive awaits are data-dependent (var_i appears in the next call expression)
uses _find_matching_paren() to find exact window for dependency check
fires when 2+ independent sequential awaits found
```

**Score deduction:** −0.25 (high)

**Conservative design:** The data dependency check prevents false positives like
`user = await get_user(); project = await get_project(user.id)` which cannot be
parallelized.

---

#### 19. DB-Access-in-Loop (N+1 Prevention)
**Location:** `src/validate.py → _check_db_in_loop()`

**What it means:** A proposed function iterates in a `for` loop AND issues a DB query
per iteration. This is O(n) queries when O(1) is available via `ANY($1)` or `executemany`.

**How it's calculated:**
```python
_DB_SINK_RE = re.compile(
    r"\b(?:_db\.execute|_pool\.acquire|conn\.fetch|conn\.execute"
    r"|asyncpg\.connect|aiosqlite\.connect)\b",
    re.IGNORECASE,
)

# Only standalone for-loop lines — NOT list comprehensions
_LOOP_RE = re.compile(r"^\s*for\s+[\w,\s(]+\s+in\b", re.MULTILINE)

fires when BOTH patterns appear in the function body
```

**Key design decision:** Uses `re.MULTILINE` with `^\s*for` so list comprehensions
like `[dict(r) for r in cur.fetchall()]` don't trigger a false positive. The full
performance.py `_LOOP_PATTERNS` matches comprehensions too — that's intentional there
because it's paired with the call graph. Here we only have raw body text.

**Score deduction:** −0.25 (high)

---

#### Score Calculation
```python
_SEVERITY_DEDUCTION = {"high": 0.25, "medium": 0.15, "low": 0.05}
score = max(0.0, 1.0 - sum(deductions))
```

| Violations | Example score |
|---|---|
| 0 | 1.00 |
| 1 medium | 0.85 |
| 1 high | 0.75 |
| 1 high + 1 medium | 0.60 |
| 2 high + 1 medium | 0.35 |
| 4 high | 0.00 |

---

## Part 2 — Signal Coverage Gaps

What the Guidance Layer currently cannot see, and why.

### Structural gaps

**A. Cross-module import boundaries**
We detect which modules functions *are* in but not which modules they *import from*.
A proposed function importing `src.embeddings` from inside `src.call_graph` may be
crossing an architectural boundary — but we have no rule for this yet. The `edges` table
has import edges; the pattern just isn't checked.

**B. Temporal coupling**
The `branch_function_changes` table records which functions changed together across
commits. If `fn_A` and `fn_B` always appear in the same commit, they're temporally coupled.
An agent editing `fn_A` should know `fn_B` is usually touched at the same time. This signal
exists in the data but has no checker.

**C. Branch conflict risk**
The same table shows when another branch modified the same function. If `fn_A` is in
the result set *and* has been modified on another branch since the project was last indexed,
there's a latent merge conflict. This is a high-value signal for multi-agent environments
that doesn't exist anywhere yet.

**D. Knowledge gap propagation**
`get_project_home` surfaces "top knowledge gaps" — functions with many callers and no
docstring/decisions. If a result set contains functions that themselves call knowledge-gap
functions, the agent is about to write code that depends on undocumented behavior.
Currently we detect knowledge gaps in the *result set*, not in the *callees of the result set*.

### Pattern gaps (validate_proposed_code)

**E. Return type consistency**
The `return_type` column exists on every node. If 7/8 existing functions return
`list[dict]` and the proposed function returns `str`, that's a conformance signal.
The check would be trivial to add.

**F. Parameter count signal**
Functions with 1–3 parameters are the norm in Phronosis. A proposed function with 7
parameters is likely doing too much or needs a dataclass. Could be checked from
`parameter_names` column on existing nodes.

**G. Decorator consistency**
`@mcp.tool()`, `@pytest.mark.asyncio`, `@property` — the `decorators` column tracks these.
If 100% of existing file functions have `@mcp.tool()` and the proposed function doesn't,
that's a missing registration.

**H. Type annotation coverage**
The `return_type` and `parameter_names` columns are populated from parsed signatures.
If existing module functions are fully annotated and proposed code has bare `def fn(x, y):`,
that's a coverage gap. Medium severity.

**I. Docstring coverage**
The `docstring` column. If existing module has >80% docstring coverage and proposed has
none, flag it. The check is: `sum(1 for n in existing if n["docstring"]) / len(existing)`.

**J. External call in loop (in validate_proposed_code)**
The full `detect_external_calls_in_loops` detector needs `callee_map` to know which
callees are external. We can't build that from proposed code alone. Partial workaround:
check if the proposed body contains a known external library name (`requests`, `aiohttp`,
`openai`, `anthropic`, `boto3`) inside a `for` loop. This would be a lower-confidence
version of signal #19 but catch HTTP-in-loop patterns.

### Inference gaps

**K. Confidence-weighted scoring**
Currently every high-severity deviation deducts 0.25 regardless of how confident the
check is. The sequential_awaits check is highly reliable (explicit regex + data-dependency
guard). The naming check fires more often on utility files where naming is genuinely mixed.
A confidence weight per check would produce a more accurate score.

**L. Module-relative thresholds**
`CHOKEPOINT_THRESHOLD = 15` is calibrated for Phronosis (~900 functions). A project with
50 functions has a different notion of "chokepoint." The threshold could be set as a
percentile of the caller-count distribution for the project rather than a fixed number.

---

## Part 3 — Potential Additions

Priority order by effort × value.

### High value, low effort

**1. Return type consistency check** (≤30 lines in validate.py)
```python
existing_return_types = Counter(n.get("return_type", "") for n in existing if n.get("return_type"))
dominant_type, ratio = existing_return_types.most_common(1)[0]
# fires when ratio >= 0.7 and proposed returns something different
```
Value: Prevents the common mistake of returning `str` from a function in a module
where everything returns `dict` or `list[dict]`.

**2. Decorator consistency check** (≤30 lines in validate.py)
```python
from src.call_graph.parser import FunctionNode
# proposed_nodes already have .decorators list from parse_file()
existing_decorators = set of decorator names across existing nodes
```
Value: Catches missing `@mcp.tool()` registrations or missing `@pytest.mark.asyncio`
in test files — both are invisible until runtime.

**3. `suggest_follow_up` in empty `get_callers`**
When `get_callers` returns 0 results, the current note says "entry point or dead code."
We could automatically check if the function has a corresponding test and surface that.
Dead code without tests is deletion-safe; dead code WITH tests means the tests call it
via a different path.

---

### High value, medium effort

**4. Temporal coupling signal** (new DB query on `branch_function_changes`)
```sql
SELECT b.function_id, COUNT(*) as co_change_count
FROM branch_function_changes a
JOIN branch_function_changes b ON a.head_commit = b.head_commit AND a.project_id = b.project_id
WHERE a.function_id = ANY($1) AND b.function_id != a.function_id
GROUP BY b.function_id
ORDER BY co_change_count DESC
LIMIT 5
```
Output: "Functions that changed alongside your results: `get_callee_map` (8×),
`upsert_node` (6×) — consider reviewing them together."

**5. Branch conflict signal** (same table, different query)
```sql
SELECT function_id FROM branch_function_changes
WHERE project_id = $1 AND function_id = ANY($2) AND branch != $3
```
Output: "Warning: `_DB.execute` was modified on branch `feature/auth` — potential
conflict if that branch merges before this change."
Especially valuable in multi-agent CI environments.

**6. External-library-in-loop for validate** (≤40 lines, partial coverage)
```python
_KNOWN_HTTP_LIBS = re.compile(r"\b(requests|aiohttp|httpx|openai|anthropic|boto3|httplib2)\b")
# fires when _LOOP_RE matches AND _KNOWN_HTTP_LIBS matches in proposed body
```
Catches "call the AI API inside a for loop" which is a common mistake agents make.

---

### Medium value, higher effort

**7. Embedding-based duplication check in validate**
Parse proposed code, embed the function body, call `query_similar` against the project.
If similarity > 0.92 to an existing function: "This function is nearly identical to
`CallGraphDB.get_all_nodes` — consider reusing or extending it."
Requires one embedding API call per validation. Worth gating behind an `--deep` flag.

**8. Module-relative CHOKEPOINT_THRESHOLD**
```sql
SELECT PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY cnt)
FROM (SELECT COUNT(DISTINCT caller_id) cnt FROM edges WHERE project_id=$1 GROUP BY callee_id)
```
The 90th percentile of caller counts becomes the project-specific chokepoint threshold.
A Phronosis-sized project gets 15; a 50k-function enterprise project gets something
proportionally higher.

**9. `module_patterns` cache population**
The `module_patterns` table exists in schema.sql but nothing writes to it. Populate it
lazily in `compute_guidance` after computing `_concentration_signal`,
`_async_signal`, and `_naming_signal` — write the results back for the dominant module.
Next call for the same module reads from the cache instead of recomputing. Enables
`validate_proposed_code` to get module conventions without fetching file nodes at all.

---

### Experimental / longer term

**10. Call chain conformance**
Parse proposed code, extract call expressions, check if they call functions that exist in
the correct module (not the wrong abstraction layer). E.g., `src.server` calling
`src.call_graph.storage._DB` directly instead of through `CallGraphDB` methods.

**11. Complexity budget signal**
Cyclomatic complexity from the proposed function body. Compare against module average.
A function that's 3× more complex than any existing function in the module is doing too much.
Cyclomatic complexity can be computed purely from the parsed AST node count.

**12. Decision memory suggestion on save**
When `validate_proposed_code` passes (score ≥ 0.8) and a contract is active, automatically
suggest calling `log_decision` with a pre-filled template linking the new function to the
relevant contract. The agent wrote conformant code — now is the right moment to record it.

---

## Implementation priority

If shipping one addition: **return type consistency** — 30 lines, zero DB cost, closes the
biggest gap in validate_proposed_code without any new infrastructure.

If shipping two: add the **temporal coupling signal** in `query_similar_functions`. It
surfaces co-change relationships that don't appear in the call graph at all — the one
category of structural signal the guidance layer completely lacks today.
