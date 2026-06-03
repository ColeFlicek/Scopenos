#!/usr/bin/env bash
# Post-commit git hook for Agent of Empires.
# Triggers authoritative incremental re-index on code-intel (TheHive) after every commit.
# Install: cp scripts/post-commit.sh .git/hooks/post-commit && chmod +x .git/hooks/post-commit

set -euo pipefail

CODE_INTEL_URL="${CODE_INTEL_URL:-http://thehive:3004}"

CHANGED=$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null || true)

if [ -z "$CHANGED" ]; then
  exit 0
fi

# Build JSON array of changed file paths (absolute paths)
REPO_ROOT=$(git rev-parse --show-toplevel)
FILES_JSON=$(echo "$CHANGED" | while IFS= read -r f; do
  [ -n "$f" ] && echo "\"${REPO_ROOT}/${f}\""
done | paste -sd ',' - | sed 's/^/[/' | sed 's/$/]/')

curl --silent --show-error --max-time 30 \
  -X POST "${CODE_INTEL_URL}/index" \
  -H "Content-Type: application/json" \
  -d "{\"changed_files\": ${FILES_JSON}}" \
  > /dev/null

echo "[code-intel] index_changes triggered for $(echo "$CHANGED" | wc -l | tr -d ' ') files"
