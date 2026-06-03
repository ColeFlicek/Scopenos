# AI Code Intel — Session Workflow

This MCP server provides structured, queryable knowledge of a codebase via call graph, semantic embeddings, and decision memory. Follow this workflow in every session.

## Before touching any function

1. `get_decision_history(function_name)` — understand why it exists in its current form
2. `get_impact_radius(function_name, depth=2)` — know the blast radius before editing
3. `query_similar_functions(snippet)` — check for parallel implementations or related patterns

## After making edits

4. `index_changes([modified_files], {file_path: content})` — keep the index fresh within this session

Pass the actual file contents as a dict: `{"path/to/file.py": "<full content>"}`.

## At session end

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

## MCP server config (Claude Code on Agent of Empires)

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "code-intel": {
      "url": "http://thehive:3004/mcp"
    }
  }
}
```

Replace `thehive` with TheHive's LAN IP if DNS is not configured.

## Git hook installation (Agent of Empires, per project)

```bash
cp /path/to/code-intel/scripts/post-commit.sh .git/hooks/post-commit
chmod +x .git/hooks/post-commit
export CODE_INTEL_URL=http://thehive:3004
```
