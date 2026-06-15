"""
File watcher for Phronosis — automatically re-indexes project files on save.

Uses watchfiles (already a transitive dependency of uvicorn) to watch
all directories registered as project roots.  On file change, runs an
incremental SCIP index (if the indexer is available for the language)
or falls back to tree-sitter via index_changes.

The watcher runs as a background asyncio task started in the FastMCP
lifespan and cancelled on shutdown.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .indexer import Indexer
    from .call_graph.storage import CallGraphDB


_SUPPORTED = {".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go",
              ".java", ".cpp", ".cc", ".hpp", ".cs", ".rb"}


async def start_file_watcher(db: "CallGraphDB", indexer: "Indexer") -> asyncio.Task:
    """Launch the file watcher as a background task and return its handle."""
    task = asyncio.create_task(_watch_loop(db, indexer), name="phronosis-file-watcher")
    return task


async def _watch_loop(db: "CallGraphDB", indexer: "Indexer") -> None:
    """Main watch loop — polls registered project roots for file changes."""
    try:
        from watchfiles import awatch
    except ImportError:
        print("[watcher] watchfiles not installed — file watcher disabled")
        return

    while True:
        # Refresh the list of watched directories from registered projects
        projects = await db.list_projects()
        roots = [p["root"] for p in projects if p.get("root") and os.path.isdir(p["root"])]
        if not roots:
            await asyncio.sleep(5)
            continue

        print(f"[watcher] watching {len(roots)} project root(s)")
        try:
            async for changes in awatch(*roots, stop_event=_make_stop_event(30)):
                await _handle_changes(changes, projects, indexer)
        except asyncio.CancelledError:
            print("[watcher] cancelled")
            return
        except Exception as exc:
            print(f"[watcher] error: {exc} — restarting in 5s")
            await asyncio.sleep(5)


def _make_stop_event(timeout_seconds: int) -> asyncio.Event:
    """Return an asyncio.Event that fires after timeout_seconds.

    This makes awatch re-evaluate the watched root list periodically so
    newly indexed projects are picked up without a server restart.
    """
    event = asyncio.Event()

    async def _set_after() -> None:
        await asyncio.sleep(timeout_seconds)
        event.set()

    asyncio.create_task(_set_after())
    return event


async def _handle_changes(
    changes: set[tuple],
    projects: list[dict],
    indexer: "Indexer",
) -> None:
    """Process a batch of file-change events from watchfiles."""
    # Group changed files by project_id
    root_to_project: dict[str, str] = {
        p["root"]: p["id"] for p in projects if p.get("root")
    }

    by_project: dict[str, list[str]] = {}
    for _change_type, file_path in changes:
        ext = Path(file_path).suffix.lower()
        if ext not in _SUPPORTED:
            continue
        for root, pid in root_to_project.items():
            if file_path.startswith(root):
                by_project.setdefault(pid, []).append(file_path)
                break

    for project_id, file_paths in by_project.items():
        project_root = next(
            (p["root"] for p in projects if p["id"] == project_id), ""
        )
        print(f"[watcher] {project_id}: {len(file_paths)} file(s) changed — re-indexing")
        try:
            file_contents: dict[str, str] = {}
            for fp in file_paths:
                try:
                    if os.path.exists(fp):
                        file_contents[fp] = Path(fp).read_text(
                            encoding="utf-8", errors="replace"
                        )
                    else:
                        file_contents[fp] = None  # deleted file
                except OSError:
                    pass

            await indexer.index_changes(
                file_paths=file_paths,
                file_contents=file_contents,
                project_root=project_root,
                project_id=project_id,
            )
            print(f"[watcher] {project_id}: incremental update done")
        except Exception as exc:
            print(f"[watcher] {project_id}: update failed: {exc}")
