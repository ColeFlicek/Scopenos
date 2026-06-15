#!/usr/bin/env bash
# ACIP — Agentic Coding Intelligence Platform — first-run setup
# Prompts for credentials, writes .env, and starts the server.

set -euo pipefail

echo ""
echo "╔════════════════════════════════════════════════╗"
echo "║   Agentic Coding Intelligence Platform — Setup  ║"
echo "╚════════════════════════════════════════════════╝"
echo ""

# ── Prerequisites ──────────────────────────────────────────────────────────────

if ! command -v docker &>/dev/null; then
  echo "Error: Docker is not installed."
  echo "       Install it from https://docs.docker.com/get-docker/"
  exit 1
fi

if ! docker compose version &>/dev/null 2>&1; then
  echo "Error: Docker Compose plugin is not installed."
  echo "       It is bundled with Docker Desktop, or install it from"
  echo "       https://docs.docker.com/compose/install/"
  exit 1
fi

if [ -f .env ]; then
  echo "A .env file already exists."
  read -rp "Overwrite and reconfigure? [y/N]: " OVERWRITE
  [[ "$OVERWRITE" =~ ^[Yy]$ ]] || exit 0
fi

# ── Anthropic API key ──────────────────────────────────────────────────────────

echo "Anthropic API key — used to generate one-line summaries per function."
echo "Get one at: https://console.anthropic.com/settings/keys"
read -rsp "Anthropic API key: " ANTHROPIC_API_KEY
echo ""
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "Error: Anthropic API key is required."
  exit 1
fi

# ── Embedding provider ─────────────────────────────────────────────────────────

echo ""
echo "Embedding provider:"
echo "  1) OpenAI   — text-embedding-3-small  (~\$0.60 per 100k functions)"
echo "  2) Ollama   — local models, free      (requires Ollama running on this machine)"
read -rp "Choose [1/2, default 1]: " PROVIDER_CHOICE
PROVIDER_CHOICE="${PROVIDER_CHOICE:-1}"

OPENAI_API_KEY=""
EMBEDDING_PROVIDER="openai"
EMBEDDING_MODEL=""
OLLAMA_BASE_URL=""

if [ "$PROVIDER_CHOICE" = "2" ]; then
  EMBEDDING_PROVIDER="ollama"
  read -rp "Ollama base URL [default: http://localhost:11434]: " OLLAMA_BASE_URL
  OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://localhost:11434}"
  read -rp "Model name      [default: nomic-embed-code]: " EMBEDDING_MODEL
  EMBEDDING_MODEL="${EMBEDDING_MODEL:-nomic-embed-code}"
else
  echo ""
  echo "OpenAI API key — used for embeddings."
  echo "Get one at: https://platform.openai.com/api-keys"
  read -rsp "OpenAI API key: " OPENAI_API_KEY
  echo ""
  if [ -z "$OPENAI_API_KEY" ]; then
    echo "Error: OpenAI API key is required for the OpenAI provider."
    exit 1
  fi
fi

# ── Write .env ─────────────────────────────────────────────────────────────────

{
  echo "ANTHROPIC_API_KEY=${ANTHROPIC_API_KEY}"
  echo "OPENAI_API_KEY=${OPENAI_API_KEY}"
  echo "EMBEDDING_PROVIDER=${EMBEDDING_PROVIDER}"
  [ -n "$EMBEDDING_MODEL"  ] && echo "EMBEDDING_MODEL=${EMBEDDING_MODEL}"
  [ -n "$OLLAMA_BASE_URL"  ] && echo "OLLAMA_BASE_URL=${OLLAMA_BASE_URL}"
} > .env

echo ""
echo "✓ Configuration written to .env"

# ── Start ──────────────────────────────────────────────────────────────────────

echo ""
read -rp "Start the server now? [Y/n]: " START_NOW
START_NOW="${START_NOW:-Y}"

if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
  echo ""
  docker compose up -d
  echo ""
  echo "✓ ACIP is running."
  echo ""
  echo "  Dashboard:     http://localhost:3004/ui"
  echo "  MCP endpoint:  http://localhost:3004/mcp"
  echo ""
  echo "Add to Claude Code MCP settings:"
  echo '  { "mcpServers": { "acip": { "url": "http://localhost:3004/mcp" } } }'
  echo ""
  echo "Then run index_project(\"/path/to/your/project\") to build the first index."
else
  echo ""
  echo "Run 'docker compose up -d' when ready."
fi
