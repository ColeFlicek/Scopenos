#!/usr/bin/env bash
# Post-commit git hook — triggers incremental re-index on the ACIP server after every commit.
# Install: cp scripts/post-commit.sh .git/hooks/post-commit && chmod +x .git/hooks/post-commit

set -euo pipefail

ACIP_URL="${ACIP_URL:-http://localhost:3004}"

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
  -X POST "${ACIP_URL}/index" \
  -H "Content-Type: application/json" \
  -d "{\"changed_files\": ${FILES_JSON}, \"project_root\": \"${REPO_ROOT}\"}" \
  > /dev/null

echo "[acip] index_changes triggered for $(echo "$CHANGED" | wc -l | tr -d ' ') files"
