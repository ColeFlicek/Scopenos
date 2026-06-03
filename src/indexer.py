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
        4. Embed all functions via OpenAI, store in neo4j.
        """
        source_files = _collect_source_files(path)
        if not source_files:
            return {"status": "no source files found", "path": path}

        all_nodes = []
        all_edges = []
        contents: dict[str, str] = {}

        for fp in source_files:
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                nodes, edges = _parser.parse_file(fp, content, project_root=path)
                all_nodes.extend(nodes)
                all_edges.extend(edges)
                contents[fp] = content
            except Exception as exc:
                print(f"[indexer] skipping {fp}: {exc}")

        # Persist call graph
        await self._db.upsert_nodes(all_nodes)
        all_ids = await self._db.get_all_node_ids()
        await self._db.upsert_edges(all_edges, all_ids)

        # Build embeddings
        chunks = []
        for fp, content in contents.items():
            chunks.extend(extract_chunks(fp, content, project_root=path))

        await self._embeddings.upsert_chunks(chunks)

        return {
            "status": "ok",
            "files_indexed": len(source_files),
            "functions_indexed": len(all_nodes),
            "edges_indexed": len(all_edges),
        }

    async def index_changes(self, file_paths: list[str], file_contents: dict[str, str]) -> dict:
        """
        Incremental update for changed files.
        1. Drop stale call graph data for each changed file.
        2. Re-parse and re-embed changed files only.
        """
        updated_nodes = []
        updated_edges = []
        updated_chunks = []

        for fp in file_paths:
            content = file_contents.get(fp)
            if content is None:
                # File deleted — purge only
                await self._db.delete_file_data(fp)
                await self._embeddings.delete_by_file(fp)
                continue

            await self._db.delete_file_data(fp)
            await self._embeddings.delete_by_file(fp)

            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue

            nodes, edges = _parser.parse_file(fp, content)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)
            updated_chunks.extend(extract_chunks(fp, content))

        if updated_nodes:
            await self._db.upsert_nodes(updated_nodes)

        if updated_edges:
            all_ids = await self._db.get_all_node_ids()
            await self._db.upsert_edges(updated_edges, all_ids)

        if updated_chunks:
            # Reuse existing summaries for unchanged functions
            existing = await self._embeddings.get_summaries([c.id for c in updated_chunks])
            await self._embeddings.upsert_chunks(updated_chunks, existing_summaries=existing)

        return {
            "status": "ok",
            "files_updated": len([fp for fp in file_paths if fp in file_contents]),
            "functions_updated": len(updated_nodes),
        }

    async def reindex_call_graph_only(
        self, file_paths: list[str], file_contents: dict[str, str]
    ) -> dict:
        """Re-parse call graph for the given files without touching embeddings."""
        updated_nodes = []
        updated_edges = []

        for fp in file_paths:
            content = file_contents.get(fp)
            await self._db.delete_file_data(fp)
            if not content:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            nodes, edges = _parser.parse_file(fp, content)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)

        if updated_nodes:
            await self._db.upsert_nodes(updated_nodes)
        if updated_edges:
            all_ids = await self._db.get_all_node_ids()
            await self._db.upsert_edges(updated_edges, all_ids)

        return {
            "status": "ok",
            "files_updated": len(file_paths),
            "nodes_updated": len(updated_nodes),
            "edges_updated": len(updated_edges),
        }

    async def reindex_embeddings_only(
        self,
        file_paths: list[str],
        file_contents: dict[str, str],
        force_summaries: bool = False,
    ) -> dict:
        """Re-embed functions for the given files without touching the call graph.
        Pass force_summaries=True to regenerate LLM summaries even for known functions."""
        updated_chunks = []

        for fp in file_paths:
            content = file_contents.get(fp)
            await self._embeddings.delete_by_file(fp)
            if not content:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            updated_chunks.extend(extract_chunks(fp, content))

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
            "files_updated": len(file_paths),
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
