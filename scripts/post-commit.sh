#!/usr/bin/env bash
# Post-commit git hook — re-indexes changed files and logs the decision to Phronosis.
# Install: cp scripts/post-commit.sh .git/hooks/post-commit && chmod +x .git/hooks/post-commit

set -euo pipefail

PHRONOSIS_URL="${PHRONOSIS_URL:-http://localhost:3004}"

CHANGED=$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null || true)

if [ -z "$CHANGED" ]; then
  exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel)

# Derive project_id: prefer PHRONOSIS_PROJECT env var, then git remote basename, then dirname.
if [ -n "${PHRONOSIS_PROJECT:-}" ]; then
  PROJECT_ID="$PHRONOSIS_PROJECT"
else
  REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)
  if [ -n "$REMOTE_URL" ]; then
    # Strip trailing .git, take last path segment
    PROJECT_ID=$(echo "$REMOTE_URL" | sed 's/\.git$//' | sed 's|.*[/:]||')
  else
    PROJECT_ID=$(basename "$REPO_ROOT")
  fi
fi

# ── Re-index changed files ──────────────────────────────────────────────────────

FILES_JSON=$(echo "$CHANGED" | while IFS= read -r f; do
  [ -n "$f" ] && echo "\"${REPO_ROOT}/${f}\""
done | paste -sd ',' - | sed 's/^/[/' | sed 's/$/]/')

curl --silent --show-error --max-time 30 \
  -X POST "${PHRONOSIS_URL}/index" \
  -H "Content-Type: application/json" \
  -d "{\"changed_files\": ${FILES_JSON}, \"project_root\": \"${REPO_ROOT}\", \"project_id\": \"${PROJECT_ID}\"}" \
  > /dev/null

echo "[phronosis] index_changes triggered for $(echo "$CHANGED" | wc -l | tr -d ' ') files (project: ${PROJECT_ID})"

# ── Contract check ──────────────────────────────────────────────────────────────
# Resolve function IDs for changed files then check against active contracts.
# Non-blocking: violations are printed as warnings but do not fail the commit.

CHANGED_SRC=$(echo "$CHANGED" | grep -E '\.(py|ts|tsx)$' || true)

if [ -n "$CHANGED_SRC" ]; then
  ABS_FILES_JSON=$(echo "$CHANGED_SRC" | while IFS= read -r f; do
    [ -n "$f" ] && echo "\"${REPO_ROOT}/${f}\""
  done | paste -sd ',' - | sed 's/^/[/' | sed 's/$/]/')

  # Get function IDs for changed files.
  FN_RESP=$(curl --silent --max-time 5 \
    -X POST "${PHRONOSIS_URL}/api/functions" \
    -H "Content-Type: application/json" \
    -d "{\"files\": ${ABS_FILES_JSON}, \"project_id\": \"${PROJECT_ID}\"}" 2>/dev/null || echo '{}')

  FUNCTION_IDS=$(echo "$FN_RESP" | python3 -c "
import sys, json
d = json.load(sys.stdin)
ids = d.get('function_ids', [])
print(json.dumps(ids))
" 2>/dev/null || echo '[]')

  # Check contracts.
  VIOLATIONS=$(curl --silent --max-time 15 \
    -X POST "${PHRONOSIS_URL}/api/contracts/check" \
    -H "Content-Type: application/json" \
    -d "{\"project_id\": \"${PROJECT_ID}\", \"function_ids\": ${FUNCTION_IDS}}" 2>/dev/null || echo '{"violations":[]}')

  VIOL_COUNT=$(echo "$VIOLATIONS" | python3 -c "
import sys, json
d = json.load(sys.stdin)
viols = d.get('violations', [])
if viols:
    print(f'[phronosis] ⚠  {len(viols)} contract violation(s) detected:')
    for v in viols:
        pct = f\"{v['score']*100:.0f}%\" if v['violation_type'] == 'semantic' else 'structural'
        print(f'  [{v[\"violation_type\"]}] {v[\"function_id\"]} → {v.get(\"contract_title\",v[\"contract_id\"])} ({pct})')
else:
    print('[phronosis] contracts: ok (no violations)')
" 2>/dev/null || echo '')

  if [ -n "$VIOL_COUNT" ]; then
    echo "$VIOL_COUNT" >&2
  fi
fi

# ── Log decision ────────────────────────────────────────────────────────────────

PHRONOSIS_PROJECT_ID="$PROJECT_ID" PHRONOSIS_URL="$PHRONOSIS_URL" REPO_ROOT="$REPO_ROOT" python3 - <<'PYEOF'
import json, os, subprocess, sys
try:
    from urllib.request import urlopen, Request as UReq

    phronosis_url    = os.environ.get("PHRONOSIS_URL", "http://localhost:3004")
    project_id  = os.environ.get("PHRONOSIS_PROJECT_ID", "default")
    repo_root   = os.environ.get("REPO_ROOT", "")

    msg   = subprocess.check_output(["git", "log", "-1", "--format=%s"]).decode().strip()
    body  = subprocess.check_output(["git", "log", "-1", "--format=%b"]).decode().strip()
    hash_ = subprocess.check_output(["git", "log", "-1", "--format=%H"]).decode().strip()
    changed = subprocess.check_output(
        ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", "HEAD"]
    ).decode().strip().splitlines()
    diff_stat = subprocess.check_output(
        ["git", "diff-tree", "--no-commit-id", "-r", "--stat", "HEAD"]
    ).decode().strip()

    low = msg.lower()
    if low.startswith(("fix", "bug", "patch", "hotfix", "revert")):
        type_ = "Patch"
    elif low.startswith(("add", "feat", "impl", "build", "create", "new", "support")):
        type_ = "Implementation"
    elif low.startswith(("refactor", "redesign", "move", "extract", "restructure", "rename", "clean")):
        type_ = "Design"
    elif low.startswith(("arch",)):
        type_ = "Architectural"
    else:
        type_ = "Patch"

    parts = [msg]
    if body:
        parts.append(body)
    parts.append(f"Changes:\n{diff_stat}")
    description = " — ".join(parts)

    # Resolve changed files → actual indexed function IDs via the Phronosis API.
    abs_files = [f"{repo_root}/{f}" for f in changed if f.endswith((".py", ".ts", ".tsx"))]
    linked = None
    if abs_files:
        try:
            fn_req = UReq(
                f"{phronosis_url}/api/functions",
                data=json.dumps({"files": abs_files, "project_id": project_id}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(fn_req, timeout=5) as r:
                fn_resp = json.loads(r.read())
            linked = fn_resp.get("function_ids") or None
        except Exception:
            linked = [f.replace("/", ".").removesuffix(".py") for f in changed
                      if f.endswith((".py", ".ts", ".tsx"))] or None

    payload = json.dumps({
        "type": type_,
        "description": description,
        "trigger": f"git:{hash_[:8]}",
        "linked_function_ids": linked,
        "project_id": project_id,
    }).encode()

    req = UReq(
        f"{phronosis_url}/api/decisions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    print(f"[phronosis] decision logged ({type_}): {resp.get('decision_id', '')[:8]}")
except Exception as e:
    print(f"[phronosis] decision log skipped: {e}", file=sys.stderr)
PYEOF
