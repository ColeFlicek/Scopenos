# Phronosis Benchmark: MCP vs File-Read Comparison

Run a structured benchmark comparing two retrieval strategies on the Phronosis codebase:

- **Session A (MCP)** — answers each question using only Phronosis MCP tools
- **Session B (Files)** — answers each question using only `Read` and `grep` (Bash)

Execute every test question under BOTH strategies, then print a comparison report.

---

## Test Questions

Answer each of the following 5 questions twice — once per strategy. Questions are ordered by increasing complexity.

**Q1 — Simple definition**
> What does `_resolve_config` do, and what is its return type?

**Q2 — Caller graph**
> What functions call `upsert_chunks`? List every caller and the file it lives in.

**Q3 — Semantic search**
> Find the 3 functions most similar to this snippet:
> ```python
> async def _embed_batch(self, texts):
>     results = []
>     for i in range(0, len(texts), 100):
>         batch = texts[i:i+100]
>         resp = await client.embeddings.create(model=self._model, input=batch)
>         results.extend(...)
>     return results
> ```

**Q4 — Impact radius**
> If `_embed_batch` changed its return type from `list[list[float]]` to a generator, what would break? List affected functions and files.

**Q5 — Decision history**
> Why does `_get()` inside `_resolve_config` treat empty strings as unset (i.e., why the `if env_val else default` guard)? What incident or constraint drove that design?

---

## Execution Protocol

### Phase 1 — MCP Strategy

For each question, answer using ONLY these tools:
- `mcp__phronosis__get_decision_history`
- `mcp__phronosis__get_impact_radius`
- `mcp__phronosis__get_callers`
- `mcp__phronosis__get_callees`
- `mcp__phronosis__query_similar_functions`
- `mcp__phronosis__query_decisions`

Do NOT read any files. Do NOT use grep or Bash.

Record for each answer:
- Tools called (names only)
- Number of tool calls made
- Whether the answer was complete/partial/not found
- Subjective quality: High / Medium / Low

### Phase 2 — File-Read Strategy

Re-answer every question using ONLY:
- `Read` (file reads)
- `Bash` with `grep`, `find`, or `git log`

Do NOT call any MCP tools.

Record for each answer:
- Files read (paths only)
- Number of tool calls made
- Whether the answer was complete/partial/not found
- Subjective quality: High / Medium / Low

---

## Output Format

After completing both phases, print this report exactly:

```
═══════════════════════════════════════════════════════════════
  Phronosis BENCHMARK REPORT — MCP vs File-Read
═══════════════════════════════════════════════════════════════

SESSION A: MCP Tools
────────────────────────────────────────────────────────────────
Q1  [quality] [tool_calls calls] [tools used]
Q2  [quality] [tool_calls calls] [tools used]
Q3  [quality] [tool_calls calls] [tools used]
Q4  [quality] [tool_calls calls] [tools used]
Q5  [quality] [tool_calls calls] [tools used]

Total tool calls: N
Questions fully answered: N/5
Questions partially answered: N/5
Questions not answered: N/5

SESSION B: File Reads + grep
────────────────────────────────────────────────────────────────
Q1  [quality] [tool_calls calls] [files read]
Q2  [quality] [tool_calls calls] [files read]
Q3  [quality] [tool_calls calls] [files read]
Q4  [quality] [tool_calls calls] [files read]
Q5  [quality] [tool_calls calls] [files read]

Total tool calls: N
Questions fully answered: N/5
Questions partially answered: N/5
Questions not answered: N/5

COMPARISON SUMMARY
────────────────────────────────────────────────────────────────
Metric                     MCP          File-Read    Winner
─────────────────────────────────────────────────────────
Total tool calls           N            N            [A|B|tie]
Fully answered             N/5          N/5          [A|B|tie]
Avg calls per question     N.N          N.N          [A|B|tie]
Unique files touched       N            N            [A|B|tie]
Q5 (decision history)      [found|miss] [found|miss] [A|B|tie]

VERDICT
────────────────────────────────────────────────────────────────
[2-3 sentence summary: which strategy was more effective, which
was more efficient, and which question showed the largest gap
between strategies — that gap is the clearest signal of Phronosis's
unique value over raw file access.]
═══════════════════════════════════════════════════════════════
```

Fill in every bracket. Do not omit any row.

After the report, log a decision summarizing the benchmark findings:

```
mcp__phronosis__log_decision(
  type="Implementation",
  description="Benchmark result: MCP vs file-read strategy comparison on 5 test questions. [paste VERDICT here]",
  rejected_alternatives=["pure file-read retrieval"],
  trigger="phronosis-benchmark slash command",
  linked_function_ids=["src.embeddings.embedder._resolve_config", "src.embeddings.embedder._embed_batch", "src.embeddings.embedder.EmbeddingStore.upsert_chunks"]
)
```
