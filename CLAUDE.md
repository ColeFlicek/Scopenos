# Scopenos — Session Workflow

Scopenos provides structured, queryable knowledge of a codebase via call graph, semantic embeddings, and decision memory. Follow this workflow in every session.

## Step 0 — Session start: build the map before anything else

On every session, use this three-tier retrieval ladder before touching any file:

**Tier 1 — one call, ~500 tokens, full architectural picture:**
```
get_project_home("project_id")
```
Returns subsystems, wiring diagram, chokepoints, entry points, risk surface,
contract compliance, churn hotspots, and recent decisions. This replaces reading
files to understand architecture. After this call you know what exists and where
the dangers are.

**Tier 2 — two phases: discover the function, then gate the edit:**

*Phase A — Discovery (task description → function name):*
```
query_similar_functions("<what you want to change>", top_k=8)
```
This is how you find *which* function to edit. Top results give you the name.
Skip Phase A only if the task already names the exact function.

*Phase B — Pre-edit gate (function name → safe to edit):*
```
get_impact_radius("<function you plan to touch>", depth=2)
get_decision_history("<function you plan to touch>")
query_similar_functions("<what you are about to write>")   # structural consistency
```
Steps 1–2 require the function name from Phase A. Step 3 is a different question:
what should the *new code you're adding* look like — not which function to edit.

**Tier 3 — file reads, only for exact implementation:**
```
Read(file, specific lines)   # only after you know exactly which function to modify
```

File reads are for one purpose only: viewing the exact implementation of a function
you are about to modify. They are not how you understand architecture.

## Step 1 — Pre-edit gate (before every Edit on an existing function)

*Precondition: you already know which function to edit. If not, run Phase A above first.*

1. `get_impact_radius(function_name, depth=2)` — what breaks if this changes?
2. `get_decision_history(function_name)` — why was this designed this way?
3. `query_similar_functions(what_you_are_about_to_write)` — what should new code look like?

Step 3 is the **structural consistency check** — it asks about the *new code you're adding*,
not about finding the target function. Inconsistent patterns create maintenance debt and
confuse future agents.

In multi-agent contexts: `get_decision_history` also reveals whether a concurrent agent
recently modified this function. Run it even on functions you wrote last session.

## Step 2 — After making edits

4. `index_changes([modified_files], {file_path: content})` — keep the index fresh within this session

Pass the actual file contents as a dict: `{"path/to/file.py": "<full content>"}`.

## Step 3 — After every git push

5. A PostToolUse hook fires automatically after `git push`. Review each pushed commit
   and call `log_decision()` for it.

For each commit:
- `git show <hash> --stat` — scope
- `git show <hash>` — diff, understand the why
- Call `log_decision()`:
  - `type`: `Architectural` | `Design` | `Implementation` | `Patch`
  - `description`: what changed AND why — not just the commit message
  - `rejected_alternatives`: what was considered and not done
  - `trigger`: `git:<short-hash>`
  - `linked_function_ids`: full function IDs for modified functions
  - `project_id`: must match the indexed project
  - `parent_decision_id`: link to a broader decision if applicable

Also call `log_decision()` immediately for any significant in-session architectural
trade-off or rejected approach that won't be captured by a commit.

## Initial setup (run once per project)

```
index_project("/absolute/path/to/project")
```

## MCP server config

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "scopenos": {
      "url": "http://localhost:3004/mcp"
    }
  }
}
```

Replace `localhost` with your server's IP or hostname if running remotely.

## Git hook installation (per project)

```bash
cp /path/to/Scopenos/scripts/post-commit.sh .git/hooks/post-commit
chmod +x .git/hooks/post-commit
export SCOPENOS_URL=http://localhost:3004
```

## Claude Code pre-edit hook (per machine, install once)

Fires before every Edit call on source files. Silently passes if Scopenos is unreachable.
Prints specific warnings when editing chokepoints or risk-surface functions.

```bash
cp /path/to/Scopenos/scripts/scopenos-pre-edit-hook.py ~/.claude/hooks/scopenos-suggest.py
```

Add to `~/.claude/settings.json` under `hooks.PreToolUse`:
```json
{
  "matcher": "Edit",
  "hooks": [{ "type": "command", "command": "python3 ~/.claude/hooks/scopenos-suggest.py" }]
}
```

# RTK
@/root/.claude/RTK.md

## Agent skills

### Issue tracker

Issues live in GitHub Issues (`ColeFlicek/Scopenos`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default five-role vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
