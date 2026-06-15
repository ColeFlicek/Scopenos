# Phronosis

A self-hosted codebase intelligence server that gives Claude Code and other MCP-compatible agents structured, queryable knowledge of any project — replacing blind file traversal with targeted retrieval.

Three layers work together: a **call graph** for structural relationships, a **semantic embedding index** for conceptual similarity, and a **decision memory** that preserves architectural context across sessions. All exposed as an MCP server any agent can call directly.

---

## How it works

| Layer | What it answers | Storage |
|---|---|---|
| Call Graph | Who calls this? What does this call? What breaks if I change it? | Postgres |
| Semantic Embeddings | What functions are conceptually similar to this snippet? | pgvector (HNSW index) |
| Decision Memory | Why was this written this way? What was tried before? | Postgres + pgvector |
| Invariant Contracts | Are any architectural rules being violated? | Postgres |

---

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) with the Compose plugin
- An [Anthropic API key](https://console.anthropic.com/settings/keys) (for one-time function summaries)
- Either an [OpenAI API key](https://platform.openai.com/api-keys) **or** [Ollama](https://ollama.com) running locally (for embeddings)

---

## Install

```bash
git clone https://github.com/ColeFlicek/Phronosis.git
cd Phronosis
./setup.sh
```

The setup script asks for your credentials and starts the full stack (Postgres + Redis + Phronosis server) via Docker Compose. No manual config editing required.

---

## Web dashboard

Once running, open **`http://localhost:3004/ui`** in your browser.

| Tab | What it shows |
|---|---|
| **Overview** | Live status for all layers — health indicators, function/edge/decision counts, indexed projects |
| **Settings** | Embedding config (provider, model, dimensions); API key presence with redacted previews |
| **Admin** | Health check tool that tests live connectivity to each layer and returns a JSON report |

API keys are never transmitted to the dashboard — only whether they are set and a redacted preview are shown.

---

## Connect Claude Code

Add to your Claude Code MCP settings:

```json
{
  "mcpServers": {
    "phronosis": {
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

This builds the call graph, generates a one-sentence summary per function (via Claude Haiku), embeds everything into pgvector, and stores it all in Postgres. Subsequent sessions use `index_changes` to stay in sync incrementally. Large index jobs run in the background — poll `GET /api/jobs/{job_id}` for status.

---

## MCP tools

```
// Indexing
index_project(path, project_id)                — full initial index; returns job_id
index_changes(file_paths, contents)            — incremental update after edits (synchronous)
reembed_project(project_id)                    — force re-embed all functions
enrich_summaries(project_id, limit)            — LLM-summarize undocumented functions
list_projects()                                — list all indexed projects

// Call graph
get_callers(function_name, project_id)         — everything that calls this function
get_callees(function_name, project_id)         — everything this function calls
get_impact_radius(function_name, depth)        — blast radius of a change, N levels out

// Semantic search
query_similar_functions(snippet, top_k)        — semantically similar functions

// Architectural intelligence
get_project_home(project_id)                   — subsystems, chokepoints, entry points, risk surface
get_function_context(query, project_id)        — unified semantic + graph + memory payload
find_dependents(symbol, project_id)            — everything that depends on this symbol

// Decision memory
log_decision(type, description, ...)           — record an architectural/design decision
get_decision_history(function_name)            — full decision lineage for a function
query_decisions(query_text)                    — semantic search over all decisions

// Invariant Contracts
create_contract(project_id, rule_type, ...)    — define an architectural rule
approve_contract(contract_id)                  — activate a contract (human gate required)
check_contracts(project_id)                    — run all active contracts, return violations
list_contracts(project_id)                     — view all contracts and status

// LSP (compiler-accurate lookups)
lsp_get_definition(file, line, column)         — go-to-definition (Jedi for Python; subprocess LSP for others)
lsp_find_references(file, line, column)        — find all references
lsp_get_diagnostics(file)                      — type errors and warnings

// Health & improvements
file_improvement(title, description, ...)      — log a bug/enhancement for a future session
list_improvements(project_id, status)          — open items across all agent sessions
resolve_improvement(improvement_id, notes)     — close an improvement

// Setup
setup_phronosis_client(project_path, project_id)    — generate setup script for a new machine/project
```

---

## Embedding models

Switch providers at any time from the **Settings** tab in the dashboard, or by editing `.env`.

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

> **Switching models** requires re-embedding all functions. Run `reembed_project(project_id)` from Claude Code — no data loss, the call graph and decision memory are unaffected.

---

## Git hook (optional)

For authoritative re-indexing on every commit, install the post-commit hook in any project you want tracked:

```bash
cp /path/to/Phronosis/scripts/post-commit.sh /your/project/.git/hooks/post-commit
chmod +x /your/project/.git/hooks/post-commit
export PHRONOSIS_URL=http://localhost:3004
```

The hook POSTs changed file paths to Phronosis after each commit.

---

## Session workflow

See [`CLAUDE.md`](CLAUDE.md) for the recommended per-session workflow (what to call before editing, after editing, and at session end).

---

## Stack

- **[FastMCP](https://github.com/jlowin/fastmcp)** — Python MCP server framework
- **[Postgres + pgvector](https://github.com/pgvector/pgvector)** — call graph, embeddings (HNSW), and decision memory
- **[Redis + RQ](https://python-rq.org)** — background indexing queue
- **[tree-sitter](https://tree-sitter.github.io)** — call graph parsing (Python, TypeScript, JavaScript, Rust, Go, Java, C++, C#, Ruby)
- **[OpenAI](https://platform.openai.com) / [Ollama](https://ollama.com)** — embeddings (your choice)
- **[Claude Haiku](https://anthropic.com)** — one-time function summary generation
- **[Jedi](https://jedi.readthedocs.io)** — Python LSP (go-to-definition, find references, diagnostics)
