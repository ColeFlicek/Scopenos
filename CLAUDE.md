# ACIP — Session Workflow

AI Code Intelligence Platform provides structured, queryable knowledge of a codebase via call graph, semantic embeddings, and decision memory. Follow this workflow in every session.

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

**Tier 2 — targeted queries for the specific feature:**
```
query_similar_functions("<feature domain>", top_k=8)
get_impact_radius("<function you plan to touch>", depth=2)
get_decision_history("<function you plan to touch>")
```

**Tier 3 — file reads, only for exact implementation:**
```
Read(file, specific lines)   # only after you know exactly which function to modify
```

File reads are for one purpose only: viewing the exact implementation of a function
you are about to modify. They are not how you understand architecture.

## Step 1 — Before modifying any specific function

1. `get_decision_history(function_name)` — understand why it exists in its current form
2. `get_impact_radius(function_name, depth=2)` — know the blast radius before editing
3. `query_similar_functions(snippet)` — check for parallel implementations

## Step 2 — After making edits

4. `index_changes([modified_files], {file_path: content})` — keep the index fresh within this session

Pass the actual file contents as a dict: `{"path/to/file.py": "<full content>"}`.

## Step 3 — At session end

5. Call `log_decision()` with a summary of any significant decisions made this session.

Fields:
- `type`: `Architectural` | `Design` | `Implementation` | `Patch`
- `description`: what was decided and why
- `rejected_alternatives`: what was considered and not chosen
- `trigger`: ticket ID, CVE, UX finding, or reason for the change
- `linked_function_ids`: full function IDs this decision governs (e.g., `src.auth.authenticate_user`)
- `parent_decision_id`: link to a broader architectural decision if applicable

## Initial setup (run once per project)

```
index_project("/absolute/path/to/project")
```

## MCP server config

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "acip": {
      "url": "http://localhost:3004/mcp"
    }
  }
}
```

Replace `localhost` with your server's IP or hostname if running remotely.

## Git hook installation (per project)

```bash
cp /path/to/ACIP/scripts/post-commit.sh .git/hooks/post-commit
chmod +x .git/hooks/post-commit
export ACIP_URL=http://localhost:3004
```

# RTK
@/root/.claude/RTK.md
