"""
LSP manager for Phronosis — compiler-accurate definition lookup, reference search,
and diagnostics.

Python analysis uses Jedi (pure Python library, no subprocess).
Other languages use a subprocess JSON-RPC LSP client that spawns the language
server on-demand, queries it, and shuts it down.  This is per-request spawning —
not efficient for interactive use but correct and stateless for MCP tool calls.

Supported language servers (must be installed in the environment):
  Python  — jedi (pip install jedi)  |  fallback: pyright --outputjson
  TypeScript/JS — typescript-language-server (npm install -g typescript-language-server)
  Rust    — rust-analyzer
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from pathlib import Path
from typing import Any


# ── Jedi (Python) ─────────────────────────────────────────────────────────────

def _jedi_available() -> bool:
    try:
        import jedi  # noqa: F401
        return True
    except ImportError:
        return False


async def python_get_definition(
    file_path: str, line: int, column: int, project_path: str = ""
) -> list[dict]:
    """Return definition location(s) for the symbol at (line, column) in file_path."""
    return await asyncio.to_thread(
        _jedi_definition_sync, file_path, line, column, project_path
    )


def _jedi_definition_sync(
    file_path: str, line: int, column: int, project_path: str
) -> list[dict]:
    try:
        import jedi
    except ImportError:
        return [{"error": "jedi not installed — run: pip install jedi"}]

    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [{"error": str(e)}]

    kwargs: dict[str, Any] = {"path": file_path}
    if project_path:
        kwargs["project"] = jedi.Project(path=project_path)

    script = jedi.Script(source, **kwargs)
    try:
        results = script.goto(line=line, column=column, follow_imports=True)
    except Exception as e:
        return [{"error": str(e)}]

    out = []
    for r in results:
        out.append({
            "name": r.name,
            "type": r.type,
            "module_path": str(r.module_path) if r.module_path else None,
            "line": r.line,
            "column": r.column,
            "description": r.description,
        })
    return out or [{"message": "no definition found"}]


async def python_find_references(
    file_path: str, line: int, column: int, project_path: str = ""
) -> list[dict]:
    """Return all references to the symbol at (line, column) in file_path."""
    return await asyncio.to_thread(
        _jedi_references_sync, file_path, line, column, project_path
    )


def _jedi_references_sync(
    file_path: str, line: int, column: int, project_path: str
) -> list[dict]:
    try:
        import jedi
    except ImportError:
        return [{"error": "jedi not installed — run: pip install jedi"}]

    try:
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return [{"error": str(e)}]

    kwargs: dict[str, Any] = {"path": file_path}
    if project_path:
        kwargs["project"] = jedi.Project(path=project_path)

    script = jedi.Script(source, **kwargs)
    try:
        results = script.get_references(line=line, column=column)
    except Exception as e:
        return [{"error": str(e)}]

    out = []
    for r in results:
        out.append({
            "name": r.name,
            "module_path": str(r.module_path) if r.module_path else None,
            "line": r.line,
            "column": r.column,
            "in_builtin_module": r.in_builtin_module,
        })
    return out or [{"message": "no references found"}]


async def python_get_diagnostics(
    file_path: str, project_path: str = ""
) -> list[dict]:
    """Run pyright on file_path and return type diagnostics.

    Falls back to a Jedi syntax check if pyright is not installed.
    """
    pyright = await _find_executable("pyright")
    if pyright:
        return await _pyright_diagnostics(pyright, file_path, project_path)
    # Jedi fallback: syntax-only check
    return await asyncio.to_thread(_jedi_diagnostics_sync, file_path)


async def _pyright_diagnostics(
    pyright_path: str, file_path: str, project_path: str
) -> list[dict]:
    """Invoke pyright --outputjson and parse the result."""
    cmd = [pyright_path, "--outputjson", file_path]
    if project_path:
        cmd += ["--project", project_path]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=project_path or None,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
        data = json.loads(stdout.decode())
    except (asyncio.TimeoutError, json.JSONDecodeError, OSError) as e:
        return [{"error": str(e)}]

    out = []
    for diag in data.get("generalDiagnostics", []):
        out.append({
            "file": diag.get("file", file_path),
            "severity": diag.get("severity", "error"),
            "message": diag.get("message", ""),
            "line": diag.get("range", {}).get("start", {}).get("line"),
            "column": diag.get("range", {}).get("start", {}).get("character"),
            "rule": diag.get("rule", ""),
        })
    return out or [{"message": "no diagnostics"}]


def _jedi_diagnostics_sync(file_path: str) -> list[dict]:
    try:
        import jedi
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        script = jedi.Script(source, path=file_path)
        errors = script.get_syntax_errors()
        return [{"severity": "error", "message": str(e), "line": e.line, "column": e.column}
                for e in errors] or [{"message": "no syntax errors"}]
    except Exception as e:
        return [{"error": str(e)}]


# ── Generic LSP subprocess client ─────────────────────────────────────────────

async def lsp_get_definition(
    file_path: str, line: int, column: int, lsp_command: list[str], workspace_root: str = ""
) -> list[dict]:
    """Spawn an LSP, initialise it, query textDocument/definition, shut it down."""
    try:
        return await _lsp_request(
            lsp_command, workspace_root, file_path,
            "textDocument/definition",
            {
                "textDocument": {"uri": _to_uri(file_path)},
                "position": {"line": line - 1, "character": column},
            },
        )
    except Exception as e:
        return [{"error": str(e)}]


async def lsp_find_references(
    file_path: str, line: int, column: int, lsp_command: list[str], workspace_root: str = ""
) -> list[dict]:
    """Spawn an LSP, query textDocument/references, shut it down."""
    try:
        return await _lsp_request(
            lsp_command, workspace_root, file_path,
            "textDocument/references",
            {
                "textDocument": {"uri": _to_uri(file_path)},
                "position": {"line": line - 1, "character": column},
                "context": {"includeDeclaration": True},
            },
        )
    except Exception as e:
        return [{"error": str(e)}]


async def _lsp_request(
    command: list[str],
    workspace_root: str,
    file_path: str,
    method: str,
    params: dict,
    timeout: float = 15.0,
) -> list[dict]:
    """Spawn an LSP server, initialise it, fire one request, parse the response."""
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=workspace_root or None,
    )

    async def send(msg_id: int | None, msg_method: str, msg_params: dict) -> None:
        obj: dict[str, Any] = {"jsonrpc": "2.0", "method": msg_method, "params": msg_params}
        if msg_id is not None:
            obj["id"] = msg_id
        body = json.dumps(obj).encode()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        assert proc.stdin is not None
        proc.stdin.write(header + body)
        await proc.stdin.drain()

    async def recv() -> dict:
        assert proc.stdout is not None
        headers: dict[str, str] = {}
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            line = raw.decode().strip()
            if not line:
                break
            k, _, v = line.partition(":")
            headers[k.strip()] = v.strip()
        length = int(headers.get("Content-Length", "0"))
        body = await asyncio.wait_for(proc.stdout.read(length), timeout=timeout)
        return json.loads(body)

    try:
        root_uri = _to_uri(workspace_root) if workspace_root else _to_uri(str(Path(file_path).parent))
        # initialize
        await send(1, "initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "capabilities": {},
            "initializationOptions": {},
        })
        resp = await recv()
        while resp.get("id") != 1:
            resp = await recv()

        await send(None, "initialized", {})

        # open the document
        source = Path(file_path).read_text(encoding="utf-8", errors="replace")
        ext = Path(file_path).suffix.lower()
        lang_id = {"py": "python", "ts": "typescript", "js": "javascript",
                   "tsx": "typescriptreact", "jsx": "javascriptreact",
                   "rs": "rust", "go": "go", "java": "java",
                   "cs": "csharp", "cpp": "cpp", "rb": "ruby"}.get(ext.lstrip("."), "plaintext")
        await send(None, "textDocument/didOpen", {
            "textDocument": {
                "uri": _to_uri(file_path),
                "languageId": lang_id,
                "version": 1,
                "text": source,
            }
        })

        # the actual query
        await send(2, method, params)
        resp = await recv()
        while resp.get("id") != 2:
            resp = await recv()

        result = resp.get("result")
        return _normalise_lsp_result(result)

    finally:
        try:
            await send(3, "shutdown", {})
            await send(None, "exit", {})
        except Exception:
            pass
        try:
            proc.terminate()
        except Exception:
            pass


def _normalise_lsp_result(result: Any) -> list[dict]:
    """Convert an LSP definition/references result to a flat list of location dicts."""
    if result is None:
        return [{"message": "no result"}]
    if isinstance(result, dict):
        result = [result]
    if not isinstance(result, list):
        return [{"raw": str(result)}]
    out = []
    for item in result:
        if isinstance(item, dict):
            loc = item.get("targetUri") or item.get("uri", "")
            if loc.startswith("file://"):
                loc = loc[7:]
            rng = item.get("targetSelectionRange") or item.get("targetRange") or item.get("range", {})
            start = rng.get("start", {})
            out.append({
                "file": loc,
                "line": start.get("line", 0) + 1,
                "column": start.get("character", 0),
            })
    return out or [{"message": "empty result"}]


# ── Utilities ─────────────────────────────────────────────────────────────────

async def _find_executable(name: str) -> str | None:
    """Return the full path to an executable if it exists on PATH."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "which", name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        path = stdout.decode().strip()
        return path if path else None
    except Exception:
        return None


def _to_uri(path: str) -> str:
    """Convert a filesystem path to a file:// URI."""
    return "file://" + os.path.abspath(path)


# ── Language-server command map ───────────────────────────────────────────────

LSP_COMMANDS: dict[str, list[str]] = {
    "typescript": ["typescript-language-server", "--stdio"],
    "javascript": ["typescript-language-server", "--stdio"],
    "rust": ["rust-analyzer"],
    "go": ["gopls"],
    "java": ["jdtls"],
    "cpp": ["clangd"],
    "csharp": ["OmniSharp", "-lsp"],
}


def lsp_command_for_file(file_path: str) -> list[str] | None:
    """Return the LSP command list for a given file extension, or None if unsupported."""
    ext = Path(file_path).suffix.lower().lstrip(".")
    lang = {
        "ts": "typescript", "tsx": "typescript",
        "js": "javascript", "jsx": "javascript",
        "rs": "rust", "go": "go", "java": "java",
        "cpp": "cpp", "cc": "cpp", "cxx": "cpp", "hpp": "cpp",
        "cs": "csharp", "rb": "ruby",
    }.get(ext)
    return LSP_COMMANDS.get(lang) if lang else None
