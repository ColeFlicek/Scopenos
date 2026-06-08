#!/usr/bin/env bash
# Post-commit git hook — re-indexes changed files and logs the decision to ACIP.
# Install: cp scripts/post-commit.sh .git/hooks/post-commit && chmod +x .git/hooks/post-commit

set -euo pipefail

ACIP_URL="${ACIP_URL:-http://localhost:3004}"

CHANGED=$(git diff-tree --no-commit-id -r --name-only HEAD 2>/dev/null || true)

if [ -z "$CHANGED" ]; then
  exit 0
fi

REPO_ROOT=$(git rev-parse --show-toplevel)

# Derive project_id: prefer ACIP_PROJECT env var, then git remote basename, then dirname.
if [ -n "${ACIP_PROJECT:-}" ]; then
  PROJECT_ID="$ACIP_PROJECT"
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
  -X POST "${ACIP_URL}/index" \
  -H "Content-Type: application/json" \
  -d "{\"changed_files\": ${FILES_JSON}, \"project_root\": \"${REPO_ROOT}\", \"project_id\": \"${PROJECT_ID}\"}" \
  > /dev/null

echo "[acip] index_changes triggered for $(echo "$CHANGED" | wc -l | tr -d ' ') files (project: ${PROJECT_ID})"

# ── Log decision ────────────────────────────────────────────────────────────────

ACIP_PROJECT_ID="$PROJECT_ID" ACIP_URL="$ACIP_URL" REPO_ROOT="$REPO_ROOT" python3 - <<'PYEOF'
import json, os, subprocess, sys
try:
    from urllib.request import urlopen, Request as UReq

    acip_url    = os.environ.get("ACIP_URL", "http://localhost:3004")
    project_id  = os.environ.get("ACIP_PROJECT_ID", "default")
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

    # Resolve changed files → actual indexed function IDs via the ACIP API.
    abs_files = [f"{repo_root}/{f}" for f in changed if f.endswith((".py", ".ts", ".tsx"))]
    linked = None
    if abs_files:
        try:
            fn_req = UReq(
                f"{acip_url}/api/functions",
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
        f"{acip_url}/api/decisions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=10) as r:
        resp = json.loads(r.read())
    print(f"[acip] decision logged ({type_}): {resp.get('decision_id', '')[:8]}")
except Exception as e:
    print(f"[acip] decision log skipped: {e}", file=sys.stderr)
PYEOF
