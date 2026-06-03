# AI Code Intel (ACIP)

A self-hosted codebase intelligence system that gives Claude Code structured, queryable knowledge of a project — replacing sequential file reads with targeted retrieval.

## Three layers

| Layer | What it captures | Storage |
|---|---|---|
| Call Graph | Function calls, imports, inheritance | SQLite |
| Semantic Embeddings | Conceptual similarity across functions | neo4j vector index |
| Decision Memory | Architectural/design decisions + lineage | Graphiti (neo4j) + SQLite |

## MCP tools

```
index_project(path)
index_changes(file_paths, file_contents)
get_callers(function_name)
get_callees(function_name)
get_impact_radius(function_name, depth)
query_similar_functions(snippet, top_k)
log_decision(type, description, rejected_alternatives, linked_function_ids, parent_decision_id)
get_decision_history(function_name)
query_decisions(query_text)
```

## Stack

- **FastMCP** — Python MCP server on port 3004
- **neo4j 5** — Graphiti backend + vector index for embeddings
- **tree-sitter** — call graph parsing (Python, TypeScript)
- **OpenAI text-embedding-3-small** — function embeddings
- **Claude Haiku** — one-time LLM summary generation per function

## Quick start

```bash
cp .env.example .env
# Fill in NEO4J_PASSWORD, OPENAI_API_KEY, ANTHROPIC_API_KEY
docker compose up -d
```

Then in Claude Code on Agent of Empires, add the MCP server:
```json
{ "mcpServers": { "code-intel": { "url": "http://thehive:3004/mcp" } } }
```

See `CLAUDE.md` for the per-session workflow.
