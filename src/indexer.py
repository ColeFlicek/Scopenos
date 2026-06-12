from __future__ import annotations
import asyncio

import os
from pathlib import Path

from .call_graph.parser import TreeSitterParser
from .call_graph.storage import CallGraphDB
from .embeddings.chunker import extract_chunks
from .embeddings.embedder import EmbeddingStore
from .lsif_import import LsifImporter
from .scip_import import ScipImporter

_SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}

_parser = TreeSitterParser()


def _derive_project_id(path: str) -> str:
    """Derive a stable project slug from a directory path (last path component)."""
    return Path(path).name or "default"


class Indexer:
    def __init__(self, db: CallGraphDB, embeddings: EmbeddingStore) -> None:
        """Store references to the call-graph database and embedding store."""
        self._db = db
        self._embeddings = embeddings

    async def index_project(self, path: str, project_id: str = "") -> dict:
        """
        Full index of a project directory.
        1. Walk source files.
        2. Parse call graph.
        3. Store nodes + edges in SQLite.
        4. Embed changed functions, store in sqlite-vec.
        """
        if not project_id:
            project_id = _derive_project_id(path)

        if not os.path.exists(path):
            return {
                "status": "path not found",
                "path": path,
                "project_id": project_id,
                "detail": (
                    f"'{path}' does not exist on the ACIP server's filesystem. "
                    "The server runs in Docker and cannot access paths on your local machine. "
                    "Use POST /api/index-bulk instead — it accepts file contents directly: "
                    '{"project_root": "...", "project_id": "...", "files": {"abs/path": "content"}}'
                ),
            }

        source_files = _collect_source_files(path)
        if not source_files:
            return {"status": "no source files found", "path": path, "project_id": project_id}

        all_nodes = []
        all_edges = []
        contents: dict[str, str] = {}
        scip_used = False

        # Try SCIP indexer first (compiler-accurate, fault-tolerant, cross-repo).
        # Falls back to tree-sitter if no SCIP indexer is installed for this language.
        primary_lang = _detect_primary_language(source_files)
        if primary_lang:
            print(f"[indexer] index_project: trying SCIP for {primary_lang!r}")
            scip_result = await _try_scip_index(path, project_id, primary_lang)
            if scip_result is not None:
                all_nodes, all_edges = scip_result
                scip_used = True
                print(f"[indexer] SCIP succeeded: {len(all_nodes)} nodes, {len(all_edges)} edges")

        if not scip_used:
            # Tree-sitter fallback: grammar-level parsing for all supported languages.
            print(f"[indexer] index_project: project={project_id!r} {len(source_files)} files under {path!r}")
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
        old_hashes: dict[str, str] = {}
        for fp in contents:
            for node in await self._db.get_nodes_by_file(fp, project_id):
                if node["summary"]:
                    existing_summaries[node["id"]] = node["summary"]
                old_hashes[node["id"]] = node.get("body_hash", "")

        # Diff: only invalidate embeddings for functions whose body changed or were removed.
        new_hashes = {n.id: n.body_hash for n in all_nodes}
        changed_ids = {nid for nid, h in new_hashes.items() if old_hashes.get(nid) != h}
        deleted_ids = set(old_hashes.keys()) - set(new_hashes.keys())
        await self._embeddings.delete_by_ids(list(changed_ids | deleted_ids), project_id)

        # Wipe and rewrite call graph for all parsed files (edges change at file granularity).
        for fp in contents:
            await self._db.delete_file_data(fp, project_id)

        print(f"[indexer] call graph: {len(all_nodes)} nodes, {len(all_edges)} edges total — writing to db")
        await self._db.upsert_nodes(all_nodes, project_id)
        all_ids = await self._db.get_all_node_ids(project_id)
        await self._db.upsert_edges(all_edges, all_ids, project_id)
        print(f"[indexer] call graph written ok")

        # Only embed functions that changed — unchanged ones keep their existing vectors.
        chunks = [
            c for fp, content in contents.items()
            for c in extract_chunks(fp, content, project_root=path)
            if c.id in changed_ids
        ]
        print(f"[indexer] {len(changed_ids)} changed, {len(deleted_ids)} removed, "
              f"{len(all_nodes) - len(changed_ids)} unchanged (skipping embed)")
        embed_stats = {"docs": 0, "fallback": 0}
        if chunks:
            print(f"[indexer] starting embedding for {len(chunks)} chunks")
            embed_stats = await self._embeddings.upsert_chunks(
                chunks,
                project_id=project_id,
                existing_summaries=existing_summaries if existing_summaries else None,
            )

        # Register / update project record.
        await self._db.upsert_project(project_id, project_id, path)

        result = {
            "status": "ok",
            "project_id": project_id,
            "structural_layer": "scip" if scip_used else "tree-sitter",
            "files_indexed": len(contents),
            "functions_indexed": len(all_nodes),
            "functions_reembedded": len(chunks),
            "edges_indexed": len(all_edges),
            "embedded_with_docs": embed_stats["docs"],
            "embedded_large_fallback": embed_stats["fallback"],
        }
        if embed_stats["fallback"]:
            result["note"] = (
                f"{embed_stats['fallback']} functions had no docstring or leading comment and were "
                f"embedded with text-embedding-3-large. Call enrich_summaries('{project_id}') to "
                f"generate LLM summaries and re-embed them with {{}}, which will improve semantic search quality."
            ).format(self._embeddings._model)
        print(f"[indexer] index_project complete: {result}")
        return result

    async def index_changes(
        self,
        file_paths: list[str],
        file_contents: dict[str, str],
        project_root: str = "",
        project_id: str = "",
    ) -> dict:
        """
        Incremental update for changed files.
        Diffs at function granularity — only re-embeds functions whose body actually changed.
        """
        if not project_id:
            project_id = _derive_project_id(project_root) if project_root else "default"

        # Snapshot existing summaries and body hashes before any deletion.
        existing_summaries: dict[str, str] = {}
        old_hashes: dict[str, str] = {}
        for fp in file_paths:
            for node in await self._db.get_nodes_by_file(fp, project_id):
                if node["summary"]:
                    existing_summaries[node["id"]] = node["summary"]
                old_hashes[node["id"]] = node.get("body_hash", "")

        updated_nodes = []
        updated_edges = []
        changed_ids: set[str] = set()

        print(f"[indexer] index_changes: project={project_id!r} {len(file_paths)} files")
        for fp in file_paths:
            content = file_contents.get(fp)

            if content is None:
                # Deleted file — wipe all its embeddings (while nodes still exist for ID lookup).
                await self._embeddings.delete_by_file(fp, project_id)
                await self._db.delete_file_data(fp, project_id)
                print(f"[indexer]   {fp}: deleted (purged from index)")
                continue

            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                await self._embeddings.delete_by_file(fp, project_id)
                await self._db.delete_file_data(fp, project_id)
                print(f"[indexer]   {fp}: skipped (unsupported extension {ext!r})")
                continue

            nodes, edges = _parser.parse_file(fp, content, project_root=project_root)
            new_hashes = {n.id: n.body_hash for n in nodes}

            # Functions whose body changed or are brand-new need a fresh embedding.
            file_changed = {nid for nid, h in new_hashes.items() if old_hashes.get(nid) != h}
            existing_file_ids = {
                n["id"] for n in await self._db.get_nodes_by_file(fp, project_id)
            }
            file_deleted = {
                nid for nid in old_hashes
                if nid in existing_file_ids and nid not in new_hashes
            }
            await self._embeddings.delete_by_ids(list(file_changed | file_deleted), project_id)

            # Refresh call graph for the whole file — edges can change in any function.
            await self._db.delete_file_data(fp, project_id)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)
            changed_ids |= file_changed
            print(f"[indexer]   {fp}: {len(nodes)} nodes, "
                  f"{len(file_changed)} to embed, {len(file_deleted)} removed, "
                  f"{len(nodes) - len(file_changed)} unchanged")

        if updated_nodes:
            print(f"[indexer] call graph: {len(updated_nodes)} nodes, {len(updated_edges)} edges — writing to db")
            await self._db.upsert_nodes(updated_nodes, project_id)
        if updated_edges:
            all_ids = await self._db.get_all_node_ids(project_id)
            await self._db.upsert_edges(updated_edges, all_ids, project_id)
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
                project_id=project_id,
                existing_summaries=existing_summaries if existing_summaries else None,
            )

        # Update project last_indexed timestamp.
        if updated_nodes or updated_chunks:
            await self._db.upsert_project(project_id, project_id, project_root)

        result = {
            "status": "ok",
            "project_id": project_id,
            "files_updated": len([fp for fp in file_paths if file_contents.get(fp) is not None]),
            "functions_updated": len(updated_nodes),
            "functions_reembedded": len(updated_chunks),
        }
        print(f"[indexer] index_changes complete: {result}")
        return result

    async def reindex_call_graph_only(
        self,
        file_paths: list[str],
        file_contents: dict[str, str],
        project_root: str = "",
        project_id: str = "",
    ) -> dict:
        """Re-parse call graph for the given files without touching embeddings."""
        if not project_id:
            project_id = _derive_project_id(project_root) if project_root else "default"

        existing_summaries: dict[str, str] = {}
        existing_node_ids: set[str] = set()
        for fp in file_paths:
            if file_contents.get(fp) is not None:
                for node in await self._db.get_nodes_by_file(fp, project_id):
                    existing_node_ids.add(node["id"])
                    if node["summary"]:
                        existing_summaries[node["id"]] = node["summary"]

        updated_nodes = []
        updated_edges = []

        for fp in file_paths:
            content = file_contents.get(fp)
            if content is None:
                await self._embeddings.delete_by_file(fp, project_id)
            await self._db.delete_file_data(fp, project_id)
            if content is None:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            nodes, edges = _parser.parse_file(fp, content, project_root=project_root)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)

        new_node_ids = {n.id for n in updated_nodes}

        if existing_node_ids:
            orphaned = [nid for nid in existing_node_ids if nid not in new_node_ids]
            if orphaned:
                await self._embeddings.delete_by_ids(orphaned, project_id)

        if updated_nodes:
            await self._db.upsert_nodes(updated_nodes, project_id)
            surviving = {nid: s for nid, s in existing_summaries.items() if nid in new_node_ids}
            if surviving:
                await self._db.batch_update_summaries(surviving, project_id)
        if updated_edges:
            all_ids = await self._db.get_all_node_ids(project_id)
            await self._db.upsert_edges(updated_edges, all_ids, project_id)

        return {
            "status": "ok",
            "project_id": project_id,
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
        project_id: str = "",
    ) -> dict:
        """Re-embed functions for the given files without touching the call graph."""
        if not project_id:
            project_id = _derive_project_id(project_root) if project_root else "default"

        updated_chunks = []

        for fp in file_paths:
            content = file_contents.get(fp)
            await self._embeddings.delete_by_file(fp, project_id)
            if content is None:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            updated_chunks.extend(extract_chunks(fp, content, project_root=project_root))

        if updated_chunks:
            existing = {} if force_summaries else await self._embeddings.get_summaries(
                [c.id for c in updated_chunks], project_id
            )
            await self._embeddings.upsert_chunks(
                updated_chunks,
                project_id=project_id,
                existing_summaries=existing,
                force_summaries=force_summaries,
            )

        return {
            "status": "ok",
            "project_id": project_id,
            "files_updated": len([fp for fp in file_paths if file_contents.get(fp) is not None]),
            "functions_reembedded": len(updated_chunks),
            "summaries_regenerated": force_summaries,
        }


    async def reembed_project(self, project_id: str) -> dict:
        """Re-embed all functions for a project using the current embedding strategy,
        without touching the call graph. Use this to migrate an existing project to
        the two-tier embedding system or to recover from a corrupted embedding state."""
        all_nodes = await self._db.get_all_nodes(project_id)
        if not all_nodes:
            return {"status": "error", "message": f"No nodes found for project '{project_id}'."}

        path = await self._db.get_project_root(project_id)
        if not path:
            return {"status": "error", "message": f"Project root not found for '{project_id}'. Re-index with index_project first."}

        # Wipe existing embeddings — every function will be re-embedded fresh.
        await self._embeddings.delete_by_ids([n["id"] for n in all_nodes], project_id)

        # Preserve any LLM-generated summaries from prior enrich_summaries runs.
        existing_summaries = {n["id"]: n["summary"] for n in all_nodes if n.get("summary")}

        # Re-parse source to get function bodies for embedding text.
        node_ids = {n["id"] for n in all_nodes}
        source_files = _collect_source_files(path)
        chunks = []
        for fp in source_files:
            try:
                content = Path(fp).read_text(encoding="utf-8", errors="replace")
                for c in extract_chunks(fp, content, project_root=path):
                    if c.id in node_ids:
                        chunks.append(c)
            except Exception as exc:
                print(f"[indexer] reembed_project: skipping {fp}: {exc}")

        if not chunks:
            # Source files not accessible (e.g. project indexed from a remote machine).
            # Fall back to building FunctionChunks from stored node metadata.
            print(f"[indexer] reembed_project: no source files at {path!r}, "
                  "embedding from stored node metadata")
            from .embeddings.chunker import FunctionChunk
            for node in all_nodes:
                chunks.append(FunctionChunk(
                    id=node["id"],
                    name=node.get("name", ""),
                    signature=node.get("signature", ""),
                    docstring=node.get("docstring", "") or "",
                    leading_comment="",
                    summary=existing_summaries.get(node["id"], ""),
                    file=node.get("file", ""),
                    module=node.get("module", ""),
                    type=node.get("type", "function"),
                    body="",
                    embed_text="",
                ))

        print(f"[indexer] reembed_project: re-embedding {len(chunks)} functions for '{project_id}'")
        embed_stats = await self._embeddings.upsert_chunks(
            chunks, project_id=project_id,
            existing_summaries=existing_summaries or None,
        )

        result = {
            "status": "ok",
            "project_id": project_id,
            "functions_reembedded": len(chunks),
            "embedded_with_docs": embed_stats["docs"],
            "embedded_large_fallback": embed_stats["fallback"],
        }
        if embed_stats["fallback"]:
            result["note"] = (
                f"{embed_stats['fallback']} functions used the large-model fallback. "
                f"Call enrich_summaries('{project_id}') to generate LLM summaries and improve search quality."
            )
        return result


    async def index_lsif(self, path: str, project_id: str = "") -> dict:
        """Ingest an LSIF NDJSON index file into ACIP.

        Extracts symbol definitions with their hover documentation and imports
        them as FunctionNode records into the call-graph + embedding pipeline.
        Call-edge resolution is deferred to a future version.

        path: filesystem path to the .lsif file on the ACIP server.
        project_id: target project namespace (defaults to lsif filename stem).
        """
        if not project_id:
            project_id = Path(path).stem or "lsif"

        try:
            importer = LsifImporter(project_root=str(Path(path).parent))
            nodes, edges = importer.parse(path)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

        if not nodes:
            return {"status": "ok", "project_id": project_id, "symbols_imported": 0,
                    "note": "No definition symbols found in LSIF file."}

        await self._db.upsert_nodes(nodes, project_id)
        all_ids = await self._db.get_all_node_ids(project_id)
        if edges:
            await self._db.upsert_edges(edges, all_ids, project_id)
        await self._db.upsert_project(project_id, project_id, str(Path(path).parent))

        chunks = []
        for n in nodes:
            from .embeddings.chunker import FunctionChunk
            chunks.append(FunctionChunk(
                id=n.id, name=n.name, signature=n.signature,
                docstring=n.docstring, leading_comment=n.leading_comment,
                summary="", file=n.file, module=n.module,
                type=n.type, body=n.body, embed_text="",
            ))
        embed_stats = {"docs": 0, "fallback": 0}
        if chunks:
            embed_stats = await self._embeddings.upsert_chunks(chunks, project_id=project_id)

        return {
            "status": "ok",
            "project_id": project_id,
            "symbols_imported": len(nodes),
            "edges_imported": len(edges),
            "embedded_with_docs": embed_stats["docs"],
            "embedded_large_fallback": embed_stats["fallback"],
        }

    async def index_scip(self, path: str, project_id: str = "") -> dict:
        """Ingest a SCIP JSON index file into ACIP.

        SCIP (Sourcegraph Code Intelligence Protocol) provides structured symbol
        information with explicit documentation and relationships.  Produces
        FunctionNode records and basic call edges from relationship data.

        path: filesystem path to the .scip.json file on the ACIP server.
        project_id: target project namespace (defaults to scip filename stem).
        """
        if not project_id:
            project_id = Path(path).stem.split(".")[0] or "scip"

        try:
            importer = ScipImporter(project_root=str(Path(path).parent))
            nodes, edges = importer.parse(path)
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

        if not nodes:
            return {"status": "ok", "project_id": project_id, "symbols_imported": 0,
                    "note": "No symbols with documentation found in SCIP file."}

        await self._db.upsert_nodes(nodes, project_id)
        all_ids = await self._db.get_all_node_ids(project_id)
        if edges:
            await self._db.upsert_edges(edges, all_ids, project_id)
        await self._db.upsert_project(project_id, project_id, str(Path(path).parent))

        chunks = []
        for n in nodes:
            from .embeddings.chunker import FunctionChunk
            chunks.append(FunctionChunk(
                id=n.id, name=n.name, signature=n.signature,
                docstring=n.docstring, leading_comment=n.leading_comment,
                summary="", file=n.file, module=n.module,
                type=n.type, body=n.body, embed_text="",
            ))
        embed_stats = {"docs": 0, "fallback": 0}
        if chunks:
            embed_stats = await self._embeddings.upsert_chunks(chunks, project_id=project_id)

        return {
            "status": "ok",
            "project_id": project_id,
            "symbols_imported": len(nodes),
            "edges_imported": len(edges),
            "embedded_with_docs": embed_stats["docs"],
            "embedded_large_fallback": embed_stats["fallback"],
        }


async def _try_scip_index(
    project_path: str,
    project_id: str,
    language: str,
) -> tuple[list, list] | None:
    """Attempt to run a SCIP indexer for the detected language.

    Returns (nodes, edges) on success, or None if the indexer is not installed
    or fails — caller falls back to tree-sitter in that case.

    Supported indexers (must be installed separately):
      Python     — scip-python  (pip install scip-python)
      TypeScript — scip-typescript  (npm install -g @sourcegraph/scip-typescript)
    """
    import subprocess
    import tempfile

    cmd_map = {
        "python": ["scip-python", "index", "--project-name", project_id, "."],
        "typescript": ["scip-typescript", "index", "--infer-tsconfig"],
        "javascript": ["scip-typescript", "index", "--infer-tsconfig"],
    }
    cmd = cmd_map.get(language)
    if not cmd:
        return None

    # Check the indexer binary exists before attempting
    try:
        check = await asyncio.create_subprocess_exec(
            cmd[0], "--version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await check.wait()
        if check.returncode not in (0, 1):  # some tools return 1 for --version
            return None
    except FileNotFoundError:
        return None

    with tempfile.TemporaryDirectory() as tmpdir:
        scip_out = os.path.join(tmpdir, "index.scip.json")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=project_path,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
            if proc.returncode != 0:
                print(f"[indexer] scip indexer exited {proc.returncode}: {stderr.decode()[:200]}")
                return None
        except (asyncio.TimeoutError, Exception) as exc:
            print(f"[indexer] scip indexer failed: {exc}")
            return None

        # scip-python outputs index.scip; convert to JSON form for ScipImporter
        scip_bin = os.path.join(project_path, "index.scip")
        if os.path.exists(scip_bin):
            try:
                conv = await asyncio.create_subprocess_exec(
                    "scip", "convert", "--from", scip_bin, "--to", "json",
                    "--output", scip_out,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(conv.communicate(), timeout=30)
                # Clean up the binary .scip file
                os.unlink(scip_bin)
            except Exception as exc:
                print(f"[indexer] scip convert failed: {exc}")
                return None
        elif not os.path.exists(scip_out):
            print("[indexer] scip indexer produced no output file")
            return None

        from .scip_import import ScipImporter
        try:
            importer = ScipImporter(project_root=project_path)
            nodes, edges = importer.parse(scip_out)
            print(f"[indexer] SCIP index: {len(nodes)} symbols, {len(edges)} edges")
            return nodes, edges
        except Exception as exc:
            print(f"[indexer] ScipImporter failed: {exc}")
            return None


def _detect_primary_language(source_files: list[str]) -> str:
    """Return the dominant language in a project based on file extension counts."""
    from collections import Counter
    counts: Counter = Counter(Path(f).suffix.lower() for f in source_files)
    priority = [".py", ".ts", ".tsx", ".js", ".jsx", ".java", ".rs", ".go", ".cpp", ".cs", ".rb"]
    for ext in priority:
        if counts.get(ext, 0) > 0:
            lang_map = {
                ".py": "python", ".ts": "typescript", ".tsx": "typescript",
                ".js": "javascript", ".jsx": "javascript", ".java": "java",
                ".rs": "rust", ".go": "go", ".cpp": "cpp", ".cs": "csharp", ".rb": "ruby",
            }
            return lang_map.get(ext, "")
    return ""


def _collect_source_files(root: str) -> list[str]:
    """Walk a directory tree and return all supported source file paths, skipping VCS/build dirs."""
    result = []
    skip_dirs = {
        ".git", ".venv", "venv", "node_modules", "__pycache__",
        ".mypy_cache", "dist", "build", ".next", ".nuxt", ".svelte-kit",
        ".turbo", "out", ".output",
    }
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip_dirs]
        for fname in filenames:
            if Path(fname).suffix.lower() in _SUPPORTED_EXTENSIONS:
                result.append(os.path.join(dirpath, fname))
    return result
