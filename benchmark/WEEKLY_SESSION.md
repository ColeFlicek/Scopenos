# Scopenos Weekly Benchmark Session

Paste this entire file into a new Claude Code session to kick off an autonomous
benchmark run. The session will run Path A (grep/file-reads only) and Path B
(Scopenos-assisted) on N curated SWE-bench tasks, evaluate both, and report a
comparison summary.

---

## Pre-flight checklist

Before spawning agents, verify the following. Abort and fix any failures.

```bash
# 1. MCP connectivity — Path B agents need Scopenos tools
python -m benchmark.run check-mcp

# 2. Pick this week's tasks (5 tasks, all categories, default seed)
python -m benchmark.run weekly-tasks --n 5

# Optional: restrict to a specific repo
# python -m benchmark.run weekly-tasks --n 5 --repos django/django pytest-dev/pytest
```

If `check-mcp` fails, Path B is degraded to grep. Fix auth before proceeding:
```bash
# On TheHive — generate a fresh API key
docker exec -it acip-postgres psql -U scopenos scopenos
# Keys are hashed — if lost, re-issue using the two-step process in the Access Topography Notion doc.
```

---

## Session instructions for Claude Code orchestrator

You are running an autonomous SWE-bench benchmark comparing:
- **Path A**: standard tools only (grep, file reads, bash)
- **Path B**: same tools PLUS Scopenos MCP (call graph, semantic search, decision memory)

### Step 1 — Load task list

```bash
python -m benchmark.run weekly-tasks --n 5
```

Parse the JSON lines. These are the tasks for this session.

### Step 2 — For each task, run both paths

For each `instance_id` from the task list:

#### 2a. Setup Path A

```bash
python -m benchmark.run setup <instance_id> --path a --results-dir benchmark/results
```

Read the `ctx.json` it writes. Note `repo_path` and `venv_python`.

Save the task to `benchmark/results/<instance_id>/task.json` if it doesn't exist:
```python
# The setup command prints a JSON object with the context
```

#### 2b. Run Path A agent

Spawn a subagent with `build_prompt_a(task, ctx)` from `benchmark/runner.py`.
Pass the prompt as the full agent instruction. The agent will:
- Explore the repo at `ctx.repo_path` with grep + file reads
- Apply a fix
- Output a final JSON block with `tool_log` and `notes`

After the agent finishes:
- Capture the diff: `git diff --unified=3` in `ctx.repo_path`
- Save to `benchmark/results/<instance_id>/path_a/patch.diff`
- Write metrics (token count, tool calls from the JSON block) via:
  ```bash
  python -m benchmark.run metrics <instance_id> --path a --tokens <N> \
    --tool-calls '<json_array>' --notes '<one_sentence>'
  ```
- Reset the repo: `git checkout -- .` in `ctx.repo_path`

#### 2c. Setup Path B

```bash
python -m benchmark.run setup <instance_id> --path b --results-dir benchmark/results
```

This re-indexes the repo at `base_commit` into Scopenos. Note `project_id` from the output.

#### 2d. Run Path B agent

Spawn a subagent with `build_prompt_b(task, ctx)` from `benchmark/runner.py`.
The agent will use Scopenos MCP tools before reading any files.

After the agent finishes:
- Capture diff → `benchmark/results/<instance_id>/path_b/patch.diff`
- Write metrics via the `metrics` command
- Reset the repo

#### 2e. Evaluate both paths

```bash
python -m benchmark.run evaluate <instance_id> --path a --results-dir benchmark/results
python -m benchmark.run evaluate <instance_id> --path b --results-dir benchmark/results
```

### Step 3 — Summary report

```bash
python -m benchmark.run summary --results-dir benchmark/results
```

Print the summary. Note:
- How many tasks each path resolved
- Scopenos call count in Path B vs file reads in Path A
- Token efficiency (Path B should use fewer file reads)

### Step 4 — Commit results

```bash
git add benchmark/results/
git commit -m "benchmark: weekly run — <date> — <N> tasks"
```

---

## Notes for the orchestrator

- **Token budget**: Each task needs ~2 subagent runs. At ~50k tokens per agent, 5 tasks ≈ 500k tokens total. Adjust `--n` based on budget.
- **Timeout**: Each subagent has a 10-minute budget (pass via the Agent tool's timeout parameter if available). Skip a task that stalls.
- **Failure handling**: If Path B setup fails (Scopenos index times out), run Path A only and mark Path B as `error: "index_failed"`.
- **Already-done tasks**: If `benchmark/results/<instance_id>/path_b/evaluation.json` exists, skip that task — it was done in a previous session.
- **Seed**: The `weekly-tasks` command uses `--seed 42` by default. Change the seed each week to get different tasks: `--seed $(date +%Y%U)` (year + week number).

---

## Quick start (copy-paste for a 3-task session)

```bash
python -m benchmark.run check-mcp && \
python -m benchmark.run weekly-tasks --n 3 --categories protocol_pair
```

Then for each task ID output, run the two-path flow above.
