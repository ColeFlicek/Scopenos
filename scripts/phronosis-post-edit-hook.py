#!/usr/bin/env python3
"""
PostToolUse hook for Edit — two responsibilities:

1. AUTO-INDEX: after every Edit on a source file, send the updated content to
   Phronosis's /api/index-bulk so the call graph and embeddings stay current without
   any manual index_changes() call. Runs in background — never slows the agent.

2. STALENESS CHECK: if the edited file is a known source of truth for content
   embedded in client_setup.py (the hook script, post-commit template, workflow
   CLAUDE.md sections), warn that client_setup.py may need to be updated so
   future setup_phronosis_client() calls distribute the latest version.

Always exits 0. Never blocks a tool call.
"""
import json
import os
import re
import subprocess
import sys

PHRONOSIS_URL = os.environ.get("PHRONOSIS_URL", "http://localhost:3004")

# Files that, when edited, may require updating embedded content in client_setup.py.
# key = path suffix to match, value = which constant / section is affected
TEMPLATE_SOURCES = {
    "scripts/phronosis-pre-edit-hook.py":  "_HOOK_SCRIPT in client_setup.py",
    "scripts/post-commit.sh":         "post-commit template (read at runtime — verify structure unchanged)",
    "src/client_setup.py":            None,  # editing the template itself — no warning needed
    "CLAUDE.md":                      "_CLIENT_CLAUDE_MD template in client_setup.py",
}


def _project_id() -> str:
    """Resolve project ID from env, git remote, or repo dirname."""
    pid = os.environ.get("PHRONOSIS_PROJECT", "")
    if pid:
        return pid
    try:
        remote = subprocess.check_output(
            ["git", "remote", "get-url", "origin"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
        return re.sub(r"\.git$", "", remote).split("/")[-1].split(":")[-1]
    except Exception:
        pass
    try:
        root = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
        return os.path.basename(root)
    except Exception:
        return ""


def _project_root() -> str:
    """Return the git repository root path, or empty string if not in a repo."""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, timeout=2
        ).decode().strip()
    except Exception:
        return ""


def _background_index(file_path: str, project_root: str, project_id: str) -> None:
    """Fire index-bulk in a background subprocess — non-blocking."""
    try:
        content = open(file_path, encoding="utf-8", errors="replace").read()
    except Exception:
        return

    index_cmd = f"""
import json, urllib.request
payload = json.dumps({{
    "project_root": {repr(project_root)},
    "project_id":   {repr(project_id)},
    "files":        {{{repr(file_path)}: {repr(content)}}},
}}).encode()
req = urllib.request.Request(
    {repr(PHRONOSIS_URL + "/api/index-bulk")},
    data=payload,
    headers={{"Content-Type": "application/json"}},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    fns = d.get("functions_updated", 0)
    emb = d.get("functions_reembedded", 0)
    print(f"[Phronosis] indexed {repr(os.path.basename(file_path))}: {{fns}} fns updated, {{emb}} re-embedded")
except Exception as e:
    print(f"[Phronosis] index update failed: {{e}}")
import os
"""
    subprocess.Popen(
        ["python3", "-c", index_cmd],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


try:
    data = json.loads(sys.stdin.read())
    tool = data.get("tool_name", "")
    inp  = data.get("tool_input", {})

    if tool != "Edit":
        sys.exit(0)

    file_path = inp.get("file_path", "")
    if not file_path:
        sys.exit(0)

    # ── 1. Auto-index source file edits ───────────────────────────────────
    if re.search(r"\.(py|ts|tsx)$", file_path):
        pid = _project_id()
        root = _project_root()
        if pid and root:
            _background_index(file_path, root, pid)
            # Brief confirmation on stdout so the agent knows it's happening
            rel = file_path.replace(root + "/", "") if root else file_path
            print(f"[Phronosis] Indexing {rel} in background...")

    # ── 2. Staleness check for client_setup.py templates ─────────────────
    for suffix, warning in TEMPLATE_SOURCES.items():
        if warning and file_path.endswith(suffix):
            print(
                f"[Phronosis] Template source edited: {suffix}\n"
                f"  → Review {warning}\n"
                f"  → Update the embedded constant so setup_phronosis_client() distributes the latest version."
            )
            break

except Exception:
    pass

sys.exit(0)
