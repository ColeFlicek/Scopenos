from __future__ import annotations

import os
from pathlib import Path

from .call_graph.parser import TreeSitterParser
from .call_graph.storage import CallGraphDB
from .embeddings.chunker import extract_chunks
from .embeddings.embedder import EmbeddingStore

_SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx"}

_parser = TreeSitterParser()


class Indexer:
    def __init__(self, db: CallGraphDB, embeddings: EmbeddingStore) -> None:
        self._db = db
        self._embeddings = embeddings

    async def index_project(self, path: str) -> dict:
        """
        Full index of a project directory.
        1. Walk source files.
        2. Parse call graph.
        3. Store nodes + edges in SQLite.
        4. Embed all functions, store in sqlite-vec.
        """
        source_files = _collect_source_files(path)
        if not source_files:
            return {"status": "no source files found", "path": path}

        all_nodes = []
        all_edges = []
        contents: dict[str, str] = {}

        print(f"[indexer] index_project: {len(source_files)} files found under {path!r}")
        for fp in source_files:
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                nodes, edges = _parser.parse_file(fp, content, project_root=path)
                all_nodes.extend(nodes)
                all_edges.extend(edges)
                contents[fp] = content
                print(f"[indexer]   parsed {fp}: {len(nodes)} nodes, {len(edges)} edges")
            except Exception as exc:
                print(f"[indexer]   skipping {fp}: {exc}")

        # Snapshot existing summaries and body hashes before deletion.
        existing_summaries: dict[str, str] = {}
        old_hashes: dict[str, str] = {}  # node_id -> body_hash
        for fp in contents:
            for node in await self._db.get_nodes_by_file(fp):
                if node["summary"]:
                    existing_summaries[node["id"]] = node["summary"]
                old_hashes[node["id"]] = node.get("body_hash", "")

        # Diff: only invalidate embeddings for functions whose body changed or were removed.
        new_hashes = {n.id: n.body_hash for n in all_nodes}
        changed_ids = {nid for nid, h in new_hashes.items() if old_hashes.get(nid) != h}
        deleted_ids = set(old_hashes.keys()) - set(new_hashes.keys())
        await self._embeddings.delete_by_ids(list(changed_ids | deleted_ids))

        # Wipe and rewrite call graph for all parsed files (edges change at file granularity).
        for fp in contents:
            await self._db.delete_file_data(fp)

        print(f"[indexer] call graph: {len(all_nodes)} nodes, {len(all_edges)} edges total — writing to db")
        await self._db.upsert_nodes(all_nodes)
        all_ids = await self._db.get_all_node_ids()
        await self._db.upsert_edges(all_edges, all_ids)
        print(f"[indexer] call graph written ok")

        # Only embed functions that changed — unchanged ones keep their existing vectors.
        chunks = [
            c for fp, content in contents.items()
            for c in extract_chunks(fp, content, project_root=path)
            if c.id in changed_ids
        ]
        print(f"[indexer] {len(changed_ids)} changed, {len(deleted_ids)} removed, "
              f"{len(all_nodes) - len(changed_ids)} unchanged (skipping embed)")
        if chunks:
            print(f"[indexer] starting embedding for {len(chunks)} chunks")
            await self._embeddings.upsert_chunks(
                chunks,
                existing_summaries=existing_summaries if existing_summaries else None,
            )

        result = {
            "status": "ok",
            "files_indexed": len(contents),
            "functions_indexed": len(all_nodes),
            "functions_reembedded": len(chunks),
            "edges_indexed": len(all_edges),
        }
        print(f"[indexer] index_project complete: {result}")
        return result

    async def index_changes(self, file_paths: list[str], file_contents: dict[str, str], project_root: str = "") -> dict:
        """
        Incremental update for changed files.
        Diffs at function granularity — only re-embeds functions whose body actually changed.
        """
        # Snapshot existing summaries and body hashes before any deletion.
        existing_summaries: dict[str, str] = {}
        old_hashes: dict[str, str] = {}  # node_id -> body_hash
        for fp in file_paths:
            for node in await self._db.get_nodes_by_file(fp):
                if node["summary"]:
                    existing_summaries[node["id"]] = node["summary"]
                old_hashes[node["id"]] = node.get("body_hash", "")

        updated_nodes = []
        updated_edges = []
        changed_ids: set[str] = set()

        print(f"[indexer] index_changes: {len(file_paths)} files, project_root={project_root!r}")
        for fp in file_paths:
            content = file_contents.get(fp)

            if content is None:
                # Deleted file — wipe all its embeddings (while nodes still exist for ID lookup).
                await self._embeddings.delete_by_file(fp)
                await self._db.delete_file_data(fp)
                print(f"[indexer]   {fp}: deleted (purged from index)")
                continue

            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                await self._embeddings.delete_by_file(fp)
                await self._db.delete_file_data(fp)
                print(f"[indexer]   {fp}: skipped (unsupported extension {ext!r})")
                continue

            nodes, edges = _parser.parse_file(fp, content, project_root=project_root)
            new_hashes = {n.id: n.body_hash for n in nodes}

            # Functions whose body changed or are brand-new need a fresh embedding.
            file_changed = {nid for nid, h in new_hashes.items() if old_hashes.get(nid) != h}
            file_deleted = {nid for nid in old_hashes if nid in
                            {n["id"] for n in await self._db.get_nodes_by_file(fp)}
                            and nid not in new_hashes}
            await self._embeddings.delete_by_ids(list(file_changed | file_deleted))

            # Refresh call graph for the whole file — edges can change in any function.
            await self._db.delete_file_data(fp)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)
            changed_ids |= file_changed
            print(f"[indexer]   {fp}: {len(nodes)} nodes, "
                  f"{len(file_changed)} to embed, {len(file_deleted)} removed, "
                  f"{len(nodes) - len(file_changed)} unchanged")

        if updated_nodes:
            print(f"[indexer] call graph: {len(updated_nodes)} nodes, {len(updated_edges)} edges — writing to db")
            await self._db.upsert_nodes(updated_nodes)
        if updated_edges:
            all_ids = await self._db.get_all_node_ids()
            await self._db.upsert_edges(updated_edges, all_ids)
        if updated_nodes or updated_edges:
            print(f"[indexer] call graph written ok")

        # Collect and embed only the changed chunks.
        updated_chunks = [
            c for fp in file_paths
            if file_contents.get(fp) and Path(fp).suffix.lower() in _SUPPORTED_EXTENSIONS
            for c in extract_chunks(fp, file_contents[fp], project_root=project_root)
            if c.id in changed_ids
        ]
        if updated_chunks:
            print(f"[indexer] starting embedding for {len(updated_chunks)} chunks")
            await self._embeddings.upsert_chunks(
                updated_chunks,
                existing_summaries=existing_summaries if existing_summaries else None,
            )

        result = {
            "status": "ok",
            "files_updated": len([fp for fp in file_paths if file_contents.get(fp) is not None]),
            "functions_updated": len(updated_nodes),
            "functions_reembedded": len(updated_chunks),
        }
        print(f"[indexer] index_changes complete: {result}")
        return result

    async def reindex_call_graph_only(
        self, file_paths: list[str], file_contents: dict[str, str], project_root: str = ""
    ) -> dict:
        """Re-parse call graph for the given files without touching embeddings."""
        # Snapshot all existing node IDs and non-empty summaries before deletion.
        # IDs are used for orphan embedding cleanup; summaries are restored after upsert.
        existing_summaries: dict[str, str] = {}
        existing_node_ids: set[str] = set()
        for fp in file_paths:
            if file_contents.get(fp) is not None:
                for node in await self._db.get_nodes_by_file(fp):
                    existing_node_ids.add(node["id"])
                    if node["summary"]:
                        existing_summaries[node["id"]] = node["summary"]

        updated_nodes = []
        updated_edges = []

        for fp in file_paths:
            content = file_contents.get(fp)
            if content is None:
                # Deleted file: clean up its embeddings before removing nodes
                # (subquery in delete_by_file needs the nodes row to resolve IDs).
                await self._embeddings.delete_by_file(fp)
            await self._db.delete_file_data(fp)
            if content is None:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            nodes, edges = _parser.parse_file(fp, content, project_root=project_root)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)

        # Compute the set of re-parsed node IDs upfront — used in both blocks below.
        new_node_ids = {n.id for n in updated_nodes}

        # Clean up embeddings for functions that no longer exist after the re-parse.
        # Use existing_node_ids (all nodes) not existing_summaries (only summarised nodes)
        # so functions with empty summaries are also cleaned up correctly.
        if existing_node_ids:
            orphaned = [nid for nid in existing_node_ids if nid not in new_node_ids]
            if orphaned:
                await self._embeddings.delete_by_ids(orphaned)

        if updated_nodes:
            await self._db.upsert_nodes(updated_nodes)
            # Restore summaries for nodes that survived the re-parse (batch, one fsync).
            surviving = {nid: s for nid, s in existing_summaries.items() if nid in new_node_ids}
            if surviving:
                await self._db.batch_update_summaries(surviving)
        if updated_edges:
            all_ids = await self._db.get_all_node_ids()
            await self._db.upsert_edges(updated_edges, all_ids)

        return {
            "status": "ok",
            "files_updated": len([fp for fp in file_paths if file_contents.get(fp) is not None]),
            "nodes_updated": len(updated_nodes),
            "edges_updated": len(updated_edges),
        }

    async def reindex_embeddings_only(
        self,
        file_paths: list[str],
        file_contents: dict[str, str],
        force_summaries: bool = False,
        project_root: str = "",
    ) -> dict:
        """Re-embed functions for the given files without touching the call graph.
        Pass force_summaries=True to regenerate LLM summaries even for known functions."""
        updated_chunks = []

        for fp in file_paths:
            content = file_contents.get(fp)
            await self._embeddings.delete_by_file(fp)
            if content is None:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            updated_chunks.extend(extract_chunks(fp, content, project_root=project_root))

        if updated_chunks:
            existing = {} if force_summaries else await self._embeddings.get_summaries(
                [c.id for c in updated_chunks]
            )
            await self._embeddings.upsert_chunks(
                updated_chunks,
                existing_summaries=existing,
                force_summaries=force_summaries,
            )

        return {
            "status": "ok",
            "files_updated": len([fp for fp in file_paths if file_contents.get(fp) is not None]),
            "functions_reembedded": len(updated_chunks),
            "summaries_regenerated": force_summaries,
        }


def _collect_source_files(root: str) -> list[str]:
    result = []
    skip_dirs = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", "dist", "build"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            if Path(fname).suffix.lower() in _SUPPORTED_EXTENSIONS:
                result.append(os.path.join(dirpath, fname))
    return result
