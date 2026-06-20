"""
Phronosis client setup — generates the self-contained Python script that configures
a user's machine and project to work with Phronosis.

Called by the setup_phronosis_client MCP tool. The returned script writes all
required files (hook, settings.json merge, CLAUDE.md, memory files, git hook)
and requires no manual steps beyond running it.
"""
from __future__ import annotations

import os

# ── Template: Claude Code pre-edit hook ───────────────────────────────────────
# Installed to ~/.claude/hooks/phronosis-suggest.py
_HOOK_SCRIPT = r'''#!/usr/bin/env python3
"""Phronosis PreToolUse hook — gate on Read, risk-check on Edit, nudge on Bash."""
import json, os, re, sys, time, urllib.request

PHRONOSIS_URL = os.environ.get("PHRONOSIS_URL", "{phronosis_url}")
TIMEOUT = 3
GATE_TTL = 1800
_GATE_DIR = os.path.expanduser("~/.claude/phronosis_gates")

def _project_id():
    pid = os.environ.get("PHRONOSIS_PROJECT", "")
    if pid: return pid
    try:
        import subprocess
        remote = subprocess.check_output(["git","remote","get-url","origin"],
            stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        return re.sub(r"\.git$","",remote).split("/")[-1].split(":")[-1]
    except Exception: pass
    try:
        import subprocess
        root = subprocess.check_output(["git","rev-parse","--show-toplevel"],
            stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        return os.path.basename(root)
    except Exception: return ""

def _project_home(pid):
    try:
        url = f"{PHRONOSIS_URL}/api/project-home/{urllib.request.quote(pid,safe='')}"
        with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
            return json.loads(r.read())
    except Exception: return {}

def _module(path):
    p = path
    for ext in (".py",".ts",".tsx",".js",".jsx"):
        if p.endswith(ext): p = p[:-len(ext)]
    return p.replace("/",".").lstrip(".")

def _gate_path(pid):
    os.makedirs(_GATE_DIR, exist_ok=True)
    return os.path.join(_GATE_DIR, re.sub(r"[^a-zA-Z0-9_-]","_",pid))

def _gate_valid(pid):
    try: return (time.time()-os.path.getmtime(_gate_path(pid))) < GATE_TTL
    except FileNotFoundError: return False

def _write_gate(pid):
    open(_gate_path(pid),"w").write(str(time.time()))

def _fmt(items, key="id", n=3):
    return ", ".join(".".join(i.get(key,"").split(".")[-2:]) for i in items[:n]) or "none"

try:
    data = json.loads(sys.stdin.read())
    tool = data.get("tool_name","")
    inp  = data.get("tool_input",{})

    if tool == "Bash":
        cmd = inp.get("command","")
        if (re.search(r"\bgrep\b",cmd) and re.search(r"\.(py|ts|tsx|js|jsx)",cmd)
                and not re.search(r"\b(git|pytest|rtk|ruff|mypy|test)\b",cmd)):
            print("[Phronosis] grep on source — MCP is faster:\n"
                  "  get_callers(fn) · get_callees(fn) · query_similar_functions(snippet)")

    elif tool == "Read":
        path = inp.get("file_path","")
        if not re.search(r"\.(py|ts|tsx|js|jsx)$",path): sys.exit(0)
        if any(x in path for x in ("/scripts/","/test","/__")): sys.exit(0)
        pid = _project_id()
        if not pid: sys.exit(0)
        if _gate_valid(pid): sys.exit(0)
        home = _project_home(pid)
        if not home:
            print("[Phronosis] Reading source — if exploring structure, MCP is faster:\n"
                  "  get_impact_radius(fn) · get_decision_history(fn) · get_callers(fn)")
            sys.exit(0)
        print(f"[Phronosis] Architectural context — {pid} ({home.get('function_count','?')} functions)")
        print(f"  Chokepoints : {_fmt(home.get('chokepoints',[]))}")
        ssl = home.get("since_last_session")
        if ssl and any(ssl.get(k) for k in ("functions_added","functions_modified","functions_removed")):
            print(f"  Since last session: +{len(ssl.get('functions_added',[]))} "
                  f"~{len(ssl.get('functions_modified',[]))} -{len(ssl.get('functions_removed',[]))} functions")
        print(f"\n[Phronosis] Context loaded. Retry your Read — gate valid for {GATE_TTL//60} min.")
        _write_gate(pid)
        sys.exit(2)

    elif tool == "Edit":
        path = inp.get("file_path","")
        if not re.search(r"\.(py|ts|tsx|js|jsx)$",path): sys.exit(0)
        mod = _module(path)
        pid = _project_id()
        if not pid: sys.exit(0)
        home = _project_home(pid)
        if not home: sys.exit(0)
        _write_gate(pid)
        warnings = []
        for cp in home.get("chokepoints",[]):
            fid = cp.get("id","")
            if mod and (mod in fid or fid.startswith(mod)):
                warnings.append(f"  CHOKEPOINT  {'.'.join(fid.split('.')[-2:])}  ({cp['caller_count']} callers)")
        if warnings:
            print(f"[Phronosis] High-risk edit in {os.path.basename(path)}:")
            for w in warnings: print(w)
            print("  1. get_impact_radius(fn, depth=2)     — what breaks?")
            print("  2. get_decision_history(fn)           — why was this designed this way?")
            print("  3. query_similar_functions(snippet)   — what is the existing pattern?")
        else:
            fn = mod.split(".")[-1] if mod else "fn"
            print(f"[Phronosis] Pre-edit: get_impact_radius({fn}) · get_decision_history({fn}) · query_similar_functions(snippet)")
except Exception: pass
sys.exit(0)
'''

# ── Template: Claude Code post-edit hook ─────────────────────────────────────
# Installed to ~/.claude/hooks/phronosis-post-edit.py
_POST_EDIT_HOOK = r'''#!/usr/bin/env python3
"""Phronosis PostToolUse hook — auto-indexes edited files and warns on template staleness."""
import json, os, re, subprocess, sys, urllib.request

PHRONOSIS_URL = os.environ.get("PHRONOSIS_URL", "{phronosis_url}")
TIMEOUT  = 3

TEMPLATE_SOURCES = {
    "scripts/phronosis-pre-edit-hook.py":  "_HOOK_SCRIPT in client_setup.py",
    "scripts/post-commit.sh":         "post-commit template (verify structure unchanged)",
    "CLAUDE.md":                      "_CLIENT_CLAUDE_MD template in client_setup.py",
}

def _project_id():
    pid = os.environ.get("PHRONOSIS_PROJECT", "")
    if pid: return pid
    try:
        remote = subprocess.check_output(["git","remote","get-url","origin"],
            stderr=subprocess.DEVNULL,timeout=2).decode().strip()
        return re.sub(r"\.git$","",remote).split("/")[-1].split(":")[-1]
    except Exception: pass
    try:
        root = subprocess.check_output(["git","rev-parse","--show-toplevel"],
            stderr=subprocess.DEVNULL,timeout=2).decode().strip()
        return os.path.basename(root)
    except Exception: return ""

def _project_root():
    try:
        return subprocess.check_output(["git","rev-parse","--show-toplevel"],
            stderr=subprocess.DEVNULL,timeout=2).decode().strip()
    except Exception: return ""

def _bg_index(file_path, root, pid):
    try: content = open(file_path, encoding="utf-8", errors="replace").read()
    except Exception: return
    cmd = f"""
import json,urllib.request
payload=json.dumps({{"project_root":{repr(root)},"project_id":{repr(pid)},"files":{{{repr(file_path)}:open({repr(file_path)},encoding="utf-8",errors="replace").read()}}}}).encode()
req=urllib.request.Request({repr(PHRONOSIS_URL+"/api/index-bulk")},data=payload,headers={{"Content-Type":"application/json"}},method="POST")
try:
    with urllib.request.urlopen(req,timeout=60) as r:
        d=json.loads(r.read())
    import os
    print(f"[Phronosis] indexed {os.path.basename({repr(file_path)})}: {{d.get('functions_updated',0)}} fns updated")
except Exception as e:
    print(f"[Phronosis] index failed: {{e}}")
"""
    subprocess.Popen(["python3","-c",cmd],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)

try:
    data = json.loads(sys.stdin.read())
    tool = data.get("tool_name","")
    inp  = data.get("tool_input",{})
    if tool != "Edit": sys.exit(0)
    path = inp.get("file_path","")
    if not path: sys.exit(0)

    if re.search(r"\.(py|ts|tsx)$", path):
        pid  = _project_id()
        root = _project_root()
        if pid and root:
            _bg_index(path, root, pid)
            rel = path.replace(root+"/","") if root else path
            print(f"[Phronosis] Indexing {rel} in background...")

    for suffix, warning in TEMPLATE_SOURCES.items():
        if path.endswith(suffix):
            print(f"[Phronosis] Template source edited: {suffix}")
            print(f"  → Review {warning}")
            print(f"  → Update the embedded constant so setup_phronosis_client() distributes the latest version.")
            break
except Exception: pass
sys.exit(0)
'''

# ── Template: project CLAUDE.md ───────────────────────────────────────────────
# Written to <project_root>/CLAUDE.md (appended if file exists)
_CLIENT_CLAUDE_MD = """\
# Phronosis Workflow

This project is indexed in Phronosis at `{phronosis_url}` (project: `{project_id}`).
Follow this three-tier retrieval ladder every session.

## Session start — build the map first

**Tier 1** (one call, ~500 tokens, full architectural picture):
```
get_project_home("{project_id}")
```
Returns subsystems, wiring, chokepoints, entry points, risk surface, contracts,
recent decisions. This replaces reading files to understand architecture.

**Tier 2** (targeted queries for the specific task):
```
query_similar_functions("<feature>", top_k=8)
get_impact_radius("<function>", depth=2)
get_decision_history("<function>")
```

**Tier 3** (file reads — precision only):
```
Read(file)   # only for exact implementation of the function you are about to modify
```

## Pre-edit gate (before every Edit on an existing function)

1. `get_impact_radius(fn, depth=2)` — what breaks?
2. `get_decision_history(fn)` — why was this designed this way?
3. `query_similar_functions(what_you_are_about_to_write)` — existing pattern?

In multi-agent contexts: step 2 also reveals whether a concurrent agent
recently modified this function. Run it even on functions you wrote yourself.

## After edits

```
index_changes(["file.py"], {{"file.py": "<content>"}})
```

## Session end

```
log_decision(type, description, trigger, linked_function_ids)
```

Log immediately after significant decisions — not only at session end.
Concurrent agents read this before touching the same code.
"""

# ── Template: memory feedback file ───────────────────────────────────────────
# Written to ~/.claude/projects/<project>/memory/feedback_phronosis_workflow.md
_MEMORY_FEEDBACK = """\
---
name: feedback-phronosis-workflow
description: "Phronosis workflow rules for {project_id}: three-tier retrieval, pre-edit gate, immediate decision logging."
metadata:
  type: feedback
---

Use the three-tier retrieval ladder on every session for project `{project_id}`:
1. get_project_home("{project_id}") — architectural map before any implementation
2. query_similar_functions / get_impact_radius / get_decision_history — specific function context
3. Read() — only for exact implementation of what you're about to modify

Pre-edit gate before every Edit: impact radius → decision history → structural consistency check.

log_decision() immediately after significant choices (not just at session end).
Concurrent agents read it before touching the same code.

**Why:** file reads for architectural understanding waste tokens and miss cross-file context.
Phronosis queries are more information-dense. [[feedback-phronosis-comprehension]]
"""

_MEMORY_INDEX = """\
# Phronosis Memory Index

- [Phronosis workflow](feedback_phronosis_workflow.md) — three-tier retrieval, pre-edit gate, immediate decision logging
"""

# ── Skill: phronosis-workflow ──────────────────────────────────────────────────────
# Installed to ~/.claude/skills/phronosis-workflow/SKILL.md
# Frontmatter is always loaded in Claude's system prompt (first level of
# progressive disclosure). Body loads when Claude judges the skill is relevant.
_Phronosis_SKILL = """\
---
name: phronosis-workflow
description: "Phronosis code intelligence for indexed codebases. Mandatory workflow: call get_project_home(project_id) FIRST every session before any source file read. Tier order: (1) get_project_home — architecture snapshot; (2) query_similar_functions / get_impact_radius / get_decision_history — function context; (3) Read — only for the exact lines you are about to modify. Never read files to understand structure; use Phronosis tools instead. Use on any Phronosis-indexed project."
---

# Phronosis Workflow

## Session Start — Three-Tier Retrieval Ladder

Run these in order at the start of every session. Do not skip Tier 1.

**Tier 1 — one call, full architectural picture (~500 tokens):**
```
get_project_home("project_id")
```
Returns: subsystems, wiring diagram, chokepoints, entry points, risk surface,
health (top knowledge gaps, contract violations, churn hotspots), recent
decisions, and a `since_last_session` diff showing what changed since the last
call. This single call replaces reading any files for architectural understanding.

**Tier 2 — targeted queries for the specific task:**
```
query_similar_functions("<feature>", top_k=8)   # find existing patterns
get_impact_radius("<function>", depth=2)         # what breaks if this changes?
get_decision_history("<function>")               # why was this designed this way?
get_callers("<function>")                        # who calls this?
get_callees("<function>")                        # what does this call?
query_decisions("<topic>")                       # prior decisions on a topic
```

**Tier 3 — file reads, precision only:**
```
Read(file, specific_lines)   # only after knowing the exact function to modify
```

## Pre-Edit Gate

Before every Edit on an existing function, run all three:

1. `get_impact_radius(fn, depth=2)` — what breaks if signature or behavior changes?
2. `get_decision_history(fn)` — why was this designed this way? what was rejected?
3. `query_similar_functions(what_you_are_about_to_write)` — existing pattern in this codebase?

Check 3 is the structural consistency check — inconsistent patterns are a class
of bugs. In multi-agent contexts, check 2 also reveals whether a concurrent
agent modified this function since your last session.

## Tool Reference

| Need | Tool |
|---|---|
| Architecture / what exists | `query_similar_functions(concept, top_k=10)` |
| Who calls this? | `get_callers(function_name)` |
| What does this call? | `get_callees(function_name)` |
| What breaks if I change X? | `get_impact_radius(function_name, depth=2)` |
| Why was this designed this way? | `get_decision_history(function_name)` |
| Prior decisions on a topic | `query_decisions(query_text)` |
| Full project snapshot | `get_project_home(project_id)` |
| What changed since last session | `get_project_home` → `since_last_session` field |

Fall back to grep or Read only if a query returns empty or the project is not indexed.

## After Edits

```
index_changes(["file.py"], {"file.py": "<full content>"})
```

## Session End

```
log_decision(
    type="Architectural | Design | Implementation | Patch",
    description="what was decided and why",
    rejected_alternatives="what was considered and not chosen",
    trigger="ticket ID, CVE, UX finding, or reason",
    linked_function_ids=["module.ClassName.method"],
    project_id="project_id"
)
```

The post-commit git hook handles commit-level decisions automatically.

## Multi-Agent Context

- `get_decision_history(fn)` before ANY edit — a concurrent agent may have just modified it
- `get_project_home` → `since_last_session` shows what changed between sessions
- `log_decision()` immediately after significant choices — the next agent reads it
- `check_contracts(project_id)` if adding new functions to an enforced project
"""

# ── Setup script generator ─────────────────────────────────────────────────────

def generate_setup_script(
    project_root: str,
    phronosis_url: str,
    project_id: str,
    claude_home: str,
    install_git_hook: bool,
    post_commit_content: str,
) -> str:
    """
    Generate a self-contained Python script that configures a machine and project
    to work with Phronosis. The caller executes this script via Bash.
    """
    hook_content = _HOOK_SCRIPT.replace("{phronosis_url}", phronosis_url)
    post_edit_content = _POST_EDIT_HOOK.replace("{phronosis_url}", phronosis_url)
    claude_md = _CLIENT_CLAUDE_MD.replace("{phronosis_url}", phronosis_url).replace("{project_id}", project_id)
    mem_feedback = _MEMORY_FEEDBACK.replace("{project_id}", project_id)
    skill_content = _Phronosis_SKILL

    mem_path_key = project_root.replace("/", "-").lstrip("-")

    pre_hook_entries = [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": f"python3 {claude_home}/hooks/phronosis-suggest.py"}]},
        {"matcher": "Read", "hooks": [{"type": "command", "command": f"python3 {claude_home}/hooks/phronosis-suggest.py"}]},
        {"matcher": "Edit", "hooks": [{"type": "command", "command": f"python3 {claude_home}/hooks/phronosis-suggest.py"}]},
    ]
    post_hook_entries = [
        {"matcher": "Edit", "hooks": [{"type": "command", "command": f"python3 {claude_home}/hooks/phronosis-post-edit.py"}]},
    ]

    import json as _json
    pre_entries_json  = _json.dumps(pre_hook_entries,  indent=4)
    post_entries_json = _json.dumps(post_hook_entries, indent=4)

    git_hook_block = ""
    if install_git_hook:
        escaped = post_commit_content.replace("\\", "\\\\").replace("'", "\\'")
        git_hook_block = f"""
# ── Git post-commit hook ──────────────────────────────────────────
git_hooks_dir = pathlib.Path("{project_root}") / ".git" / "hooks"
if git_hooks_dir.exists():
    post_commit = git_hooks_dir / "post-commit"
    post_commit.write_text({repr(post_commit_content)})
    post_commit.chmod(post_commit.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    results.append(f"  git hook     {{post_commit}}")
else:
    results.append("  git hook     SKIPPED (no .git/hooks found)")
"""

    script = f'''#!/usr/bin/env python3
"""Phronosis client setup — generated by setup_phronosis_client().  Run once per machine/project."""
import json, os, pathlib, re, stat, sys

PROJECT_ROOT = "{project_root}"
PHRONOSIS_URL     = "{phronosis_url}"
PROJECT_ID   = "{project_id}"
CLAUDE_HOME  = "{claude_home}"
MEM_KEY      = "{mem_path_key}"

results = []

# ── Pre-edit and post-edit hooks ──────────────────────────────────
hooks_dir = pathlib.Path(CLAUDE_HOME) / "hooks"
hooks_dir.mkdir(parents=True, exist_ok=True)
for fname, content in [("phronosis-suggest.py", {repr(hook_content)}),
                        ("phronosis-post-edit.py", {repr(post_edit_content)})]:
    p = hooks_dir / fname
    p.write_text(content)
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
results.append(f"  hooks        {{hooks_dir}}/phronosis-suggest.py + phronosis-post-edit.py")

# ── settings.json — merge PreToolUse and PostToolUse entries ──────
settings_path = pathlib.Path(CLAUDE_HOME) / "settings.json"
settings = json.loads(settings_path.read_text()) if settings_path.exists() else {{}}

pre_entries  = {pre_entries_json}
post_entries = {post_entries_json}

hooks_cfg = settings.setdefault("hooks", {{}})

pre_list  = hooks_cfg.setdefault("PreToolUse",  [])
pre_matchers = {{h["matcher"] for h in pre_list}}
for entry in pre_entries:
    if entry["matcher"] not in pre_matchers:
        pre_list.append(entry)

post_list = hooks_cfg.setdefault("PostToolUse", [])
post_matchers = {{h["matcher"] for h in post_list}}
for entry in post_entries:
    if entry["matcher"] not in post_matchers:
        post_list.append(entry)

settings_path.write_text(json.dumps(settings, indent=2))
results.append(f"  settings     {{settings_path}}")

# ── Project CLAUDE.md ──────────────────────────────────────────────
claude_md_path = pathlib.Path(PROJECT_ROOT) / "CLAUDE.md"
phronosis_section = {repr(claude_md)}
if claude_md_path.exists():
    existing = claude_md_path.read_text()
    if "# Phronosis Workflow" not in existing:
        claude_md_path.write_text(existing.rstrip() + "\\n\\n" + phronosis_section)
        results.append(f"  CLAUDE.md    {{claude_md_path}} (Phronosis section appended)")
    else:
        results.append(f"  CLAUDE.md    {{claude_md_path}} (already has Phronosis section, skipped)")
else:
    claude_md_path.write_text(phronosis_section)
    results.append(f"  CLAUDE.md    {{claude_md_path}} (created)")

# ── Memory files ───────────────────────────────────────────────────
mem_dir = pathlib.Path(CLAUDE_HOME) / "projects" / MEM_KEY / "memory"
mem_dir.mkdir(parents=True, exist_ok=True)
(mem_dir / "feedback_phronosis_workflow.md").write_text({repr(mem_feedback)})
mem_index = mem_dir / "MEMORY.md"
if not mem_index.exists():
    mem_index.write_text({repr(_MEMORY_INDEX)})
results.append(f"  memory       {{mem_dir}}")

# ── Skill: phronosis-workflow ───────────────────────────────────────────
skill_dir = pathlib.Path(CLAUDE_HOME) / "skills" / "phronosis-workflow"
skill_dir.mkdir(parents=True, exist_ok=True)
(skill_dir / "SKILL.md").write_text({repr(skill_content)})
results.append(f"  skill        {{skill_dir / 'SKILL.md'}}")

{git_hook_block}

# ── Done ───────────────────────────────────────────────────────────
print("\\nPhronosis setup complete for project:", PROJECT_ID)
print("Server:", PHRONOSIS_URL)
print("\\nFiles written:")
for r in results: print(r)
print("\\nNext: restart Claude Code to activate the hooks.")
print("Then run: index_project(\\"" + PROJECT_ROOT + "\\") to index the codebase.")
'''
    return script


def _default_claude_home() -> str:
    """Return the default Claude home directory path (~/.claude)."""
    return os.path.expanduser("~/.claude")
