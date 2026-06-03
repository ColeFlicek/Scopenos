# AI Code Intel

A self-hosted codebase intelligence server that gives Claude Code structured, queryable knowledge of any project — replacing blind file traversal with targeted retrieval.

Three layers work together: a **call graph** for structural relationships, a **semantic embedding index** for conceptual similarity, and a **decision memory** that preserves architectural context across sessions. All exposed as an MCP server Claude Code can call directly.

---

## How it works

| Layer | What it answers | Storage |
|---|---|---|
| Call Graph | Who calls this? What does this call? What breaks if I change it? | SQLite |
| Semantic Embeddings | What functions are conceptually similar to this snippet? | neo4j vector index |
| Decision Memory | Why was this written this way? What was tried before? | Graphiti + SQLite |

## MCP tools

```
index_project(path)                    — full initial index of a project directory
index_changes(file_paths, contents)    — incremental update after edits
get_callers(function_name)             — everything that calls this function
get_callees(function_name)             — everything this function calls
get_impact_radius(function_name, depth)— blast radius of a change, N levels out
query_similar_functions(snippet, top_k)— semantically similar functions
log_decision(type, description, ...)   — record an architectural/design decision
get_decision_history(function_name)    — full decision lineage for a function
query_decisions(query_text)            — semantic search over all decisions
```

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with the Compose plugin
- An [Anthropic API key](https://console.anthropic.com/settings/keys) (for one-time function summaries)
- Either an [OpenAI API key](https://platform.openai.com/api-keys) **or** [Ollama](https://ollama.com) running locally (for embeddings)

---

## Install

```bash
git clone https://github.com/ColeFlicek/ACIP.git
cd ACIP
./setup.sh
```

The setup script asks for your credentials, picks an embedding provider, and starts the server. No manual config file editing required.

---

## Connect Claude Code

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "code-intel": {
      "url": "http://localhost:3004/mcp"
    }
  }
}
```

If running on a remote machine, replace `localhost` with its IP or hostname.

---

## Index a project

Once the server is running, call from Claude Code:

```
index_project("/absolute/path/to/your/project")
```

This builds the call graph, generates a one-sentence summary per function (via Claude Haiku), embeds everything, and stores it all. Subsequent sessions use `index_changes` to stay in sync incrementally.

---

## Embedding models

The embedding provider and model are controlled by environment variables — edit `.env` to switch, then restart with `docker compose restart code-intel`.

| Variable | Default | Description |
|---|---|---|
| `EMBEDDING_PROVIDER` | `openai` | `openai` or `ollama` |
| `EMBEDDING_MODEL` | provider default | Model name |
| `EMBEDDING_DIM` | auto-inferred | Vector dimensions — only needed for unknown models |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server URL |

### Supported models (dimensions auto-inferred)

| Provider | Model | Dimensions |
|---|---|---|
| `openai` | `text-embedding-3-small` *(default)* | 1536 |
| `openai` | `text-embedding-3-large` | 3072 |
| `openai` | `text-embedding-ada-002` | 1536 |
| `ollama` | `nomic-embed-code` *(default)* | 768 |
| `ollama` | `nomic-embed-text` | 768 |
| `ollama` | `mxbai-embed-large` | 1024 |

Any model served by Ollama's OpenAI-compatible endpoint works — set `EMBEDDING_DIM` explicitly if it isn't in the table above.

> **Switching models after initial setup** requires wiping the vector index: `docker compose down -v && ./setup.sh`, then re-run `index_project`.

---

## Git hook (optional)

For authoritative re-indexing on every commit, install the post-commit hook in any project you want tracked:

```bash
cp /path/to/code-intel/scripts/post-commit.sh /your/project/.git/hooks/post-commit
chmod +x /your/project/.git/hooks/post-commit
```

The hook POSTs changed file paths to the `/index` endpoint after each commit.

---

## Session workflow

See [`CLAUDE.md`](CLAUDE.md) for the recommended per-session workflow (what to call before editing, after editing, and at session end).

---

## Stack

- **[FastMCP](https://github.com/jlowin/fastmcp)** — Python MCP server framework
- **[neo4j 5](https://neo4j.com)** — graph database + native vector index
- **[Graphiti](https://github.com/getzep/graphiti)** — episodic memory for decision storage
- **[tree-sitter](https://tree-sitter.github.io)** — call graph parsing (Python, TypeScript)
- **[OpenAI](https://platform.openai.com) / [Ollama](https://ollama.com)** — embeddings (your choice)
- **[Claude Haiku](https://anthropic.com)** — one-time function summary generation
