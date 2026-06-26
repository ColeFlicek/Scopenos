from __future__ import annotations
import asyncio
import json
import os
import re
import uuid
from pathlib import Path

from .branch_tracking import BranchContext, detect_branch
from .call_graph.parser import TreeSitterParser
from .call_graph.storage import CallGraphDB, derive_schema_name
from .dependency_fingerprint import DependencyFingerprint, DependencyFingerprinter
from .embeddings.chunker import extract_chunks
from .embeddings.pipeline import EmbeddingPipeline
from .index_delta import IndexDelta, reconcile
from .index_coverage import IndexCoverage
from .lsif_import import LsifImporter
from .scip_import import ScipImporter

_SUPPORTED_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx"}

# Calibrated from 8 demo repos (seaborn → django).
# Regex undercounts functions by ~28% vs tree-sitter — correct with 1.3x factor.
# Lower bound: small repos without heavy SCIP augmentation (~0.025s/fn corrected).
# Upper bound: large Python repos with full scip-python run (~0.085s/fn corrected).
# All 8 actuals land within this range.
_FN_REGEX_CORRECTION = 1.3     # regex misses lambdas, nested defs, decorators
_SECONDS_PER_FN_LOW  = 0.025   # fast case: small repo / SCIP skipped
_SECONDS_PER_FN_HIGH = 0.085   # slow case: large repo + full scip-python run
# Schema objects embed in a single batch call — ~0.005s per class, minimum 5s.
_SECONDS_PER_CLASS = 0.005
_SCHEMA_MIN_SECONDS = 5

# Fast regex patterns for pre-index function count estimate — one pass, no AST.
_FUNCTION_SCAN_RE: dict[str, re.Pattern] = {
    ".py":  re.compile(r"^\s*(async\s+)?def\s+\w", re.MULTILINE),
    ".ts":  re.compile(r"\bfunction\s+\w|const\s+\w+\s*=\s*(?:async\s+)?\(", re.MULTILINE),
    ".tsx": re.compile(r"\bfunction\s+\w|const\s+\w+\s*=\s*(?:async\s+)?\(", re.MULTILINE),
    ".js":  re.compile(r"\bfunction\s+\w|\w+\s*=\s*(?:async\s+)?(?:function\b|\()", re.MULTILINE),
    ".jsx": re.compile(r"\bfunction\s+\w|\w+\s*=\s*(?:async\s+)?(?:function\b|\()", re.MULTILINE),
}

_CLASS_SCAN_RE: dict[str, re.Pattern] = {
    ".py":  re.compile(r"^\s*class\s+\w", re.MULTILINE),
    ".ts":  re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+\w", re.MULTILINE),
    ".tsx": re.compile(r"^\s*(?:export\s+)?(?:abstract\s+)?class\s+\w", re.MULTILINE),
    ".js":  re.compile(r"^\s*class\s+\w", re.MULTILINE),
    ".jsx": re.compile(r"^\s*class\s+\w", re.MULTILINE),
}

_parser = TreeSitterParser()
_fingerprinter = DependencyFingerprinter()


def _derive_project_id(path: str) -> str:
    """Derive a stable project slug from a directory path (last path component)."""
    return Path(path).name or "default"


def estimate_project(path: str) -> dict:
    """Fast pre-scan: count functions and classes by regex to estimate index time.

    Runs in < 1s on any project size — no DB, no embedding, no tree-sitter.
    Covers two phases: call graph + function embeddings (~0.037s/fn) and
    schema object embeddings (~0.005s/class, min 5s).

    Returns:
        files, estimated_functions, estimated_classes, estimated_seconds,
        breakdown: {call_graph_embedding, schema_objects}
    """
    source_files = _collect_source_files(path)
    total_fns = 0
    total_cls = 0
    total_lines = 0
    for fp in source_files:
        ext = Path(fp).suffix.lower()
        try:
            text = Path(fp).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        total_lines += text.count("\n")
        fn_pat = _FUNCTION_SCAN_RE.get(ext)
        if fn_pat:
            total_fns += len(fn_pat.findall(text))
        cls_pat = _CLASS_SCAN_RE.get(ext)
        if cls_pat:
            total_cls += len(cls_pat.findall(text))

    corrected_fns = int(total_fns * _FN_REGEX_CORRECTION)
    schema_seconds = max(_SCHEMA_MIN_SECONDS, int(total_cls * _SECONDS_PER_CLASS))
    total_low  = int(corrected_fns * _SECONDS_PER_FN_LOW)  + schema_seconds
    total_high = int(corrected_fns * _SECONDS_PER_FN_HIGH) + schema_seconds

    def _fmt(s: int) -> str:
        return f"~{s // 60}m {s % 60}s" if s >= 60 else f"~{s}s"

    return {
        "files": len(source_files),
        "lines": total_lines,
        "estimated_functions": total_fns,
        "estimated_classes": total_cls,
        "estimated_seconds_low": total_low,
        "estimated_seconds_high": total_high,
        "estimated_time": f"{_fmt(total_low)} – {_fmt(total_high)}",
        "note": "Upper bound applies if scip-python augmentation runs on a large Python repo.",
    }




class Indexer:
    def __init__(self, db: CallGraphDB, pipeline: EmbeddingPipeline) -> None:
        self._db = db
        self._pipeline = pipeline

    async def index_project(self, path: str, project_id: str = "", branch: str = "") -> dict:
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
                    f"'{path}' does not exist on the Scopenos server's filesystem. "
                    "The server runs in Docker and cannot access paths on your local machine. "
                    "Use POST /api/index-bulk instead — it accepts file contents directly: "
                    '{"project_root": "...", "project_id": "...", "files": {"abs/path": "content"}}'
                ),
            }

        source_files = _collect_source_files(path)
        if not source_files:
            return {"status": "no source files found", "path": path, "project_id": project_id}

        # Ensure the project schema exists and get a project-scoped DB + pipeline.
        # All per-project writes (nodes, edges, embeddings) go through pdb so that
        # search_path routes them into the project schema, not public.
        schema_name = derive_schema_name(project_id)
        await self._db.create_project_schema(schema_name)
        pdb = await self._db.project_db(schema_name)
        ppipe = self._pipeline.with_db(pdb)

        all_nodes = []
        all_edges = []
        contents: dict[str, str] = {}

        # Phase 1: Tree-sitter always runs — source of truth for internal nodes,
        # body text, and content-based hashes.
        print(f"[indexer] index_project: project={project_id!r} schema={schema_name!r} "
              f"{len(source_files)} files under {path!r}")
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

        # Phase 2: SCIP augments with external dependency nodes and reference edges.
        # External nodes (is_external=True) are reference anchors in the call graph —
        # they enable get_callers("openai.X") and get_impact_radius across package boundaries.
        # Internal SCIP nodes are ignored — tree-sitter already handled them with full fidelity.
        scip_external_count = 0
        scip_ref_edge_count = 0
        primary_lang = _detect_primary_language(source_files)
        if primary_lang:
            print(f"[indexer] augmenting with SCIP for {primary_lang!r}")
            scip_result = await _try_scip_index(path, project_id, primary_lang)
            if scip_result is not None:
                scip_nodes, scip_edges = scip_result
                external_nodes = [n for n in scip_nodes if n.is_external]
                all_nodes.extend(external_nodes)
                all_edges.extend(scip_edges)
                scip_external_count = len(external_nodes)
                scip_ref_edge_count = len(scip_edges)
                print(f"[indexer] SCIP: +{scip_external_count} external nodes, "
                      f"+{scip_ref_edge_count} reference edges")

        # Snapshot existing summaries and body hashes before deletion.
        existing_summaries: dict[str, str] = {}
        old_hashes: dict[str, str] = {}
        for fp in contents:
            for node in await pdb.get_nodes_by_file(fp, project_id):
                if node["summary"]:
                    existing_summaries[node["id"]] = node["summary"]
                old_hashes[node["id"]] = node.get("body_hash", "")

        # Reconcile: diff old DB state against freshly parsed nodes.
        new_hashes = {n.id: n.body_hash for n in all_nodes}
        delta = reconcile(old_hashes, new_hashes)
        await ppipe.delete_by_ids(list(delta.to_delete), project_id)

        # Drop stale Claude summaries for functions whose body changed. A summary
        # generated when a function had no docstring becomes misleading if a good
        # docstring has since been added. Clearing it lets the pipeline treat the
        # function on its own docstring merit and lets enrich_summaries regenerate
        # a fresh summary on the next explicit run.
        for fn_id in delta.to_embed:
            existing_summaries.pop(fn_id, None)

        # Wipe and rewrite call graph for all parsed files (edges change at file granularity).
        for fp in contents:
            await pdb.delete_file_data(fp, project_id)

        print(f"[indexer] call graph: {len(all_nodes)} nodes, {len(all_edges)} edges total — writing to db")
        await pdb.upsert_nodes(all_nodes, project_id)
        all_ids = await pdb.get_all_node_ids(project_id)
        await pdb.upsert_edges(all_edges, all_ids, project_id)
        print(f"[indexer] call graph written ok")

        print(f"[indexer] +{len(delta.functions_added)} new, "
              f"~{len(delta.functions_changed)} changed, "
              f"-{len(delta.functions_removed)} removed, "
              f"{len(delta.unchanged)} unchanged (skipping embed)")
        embed_stats, fp_result = await self._finalize_index(
            project_id=project_id,
            project_root=path,
            branch_override=branch,
            ids_to_embed=delta.to_embed,
            changed_fn_ids=list(delta.functions_added | delta.functions_changed),
            existing_summaries=existing_summaries,
            contents=contents,
            always_save_fingerprint=True,
            capture_fingerprint=True,
            project_db=pdb,
            project_pipeline=ppipe,
        )

        internal_count = sum(1 for n in all_nodes if not n.is_external)
        result = {
            "status": "ok",
            "project_id": project_id,
            "structural_layer": "tree-sitter" + ("+scip" if scip_external_count else ""),
            "files_indexed": len(contents),
            "functions_indexed": internal_count,
            "external_nodes": scip_external_count,
            "scip_reference_edges": scip_ref_edge_count,
            "functions_new": len(delta.functions_added),
            "functions_changed": len(delta.functions_changed),
            "functions_removed": len(delta.functions_removed),
            "functions_unchanged": len(delta.unchanged),
            "functions_reembedded": len(delta.to_embed),
            "edges_indexed": len(all_edges),
            "embedded_with_docs": embed_stats["docs"],
            "embedded_large_fallback": embed_stats["fallback"],
        }
        if embed_stats["fallback"]:
            result["note"] = (
                f"{embed_stats['fallback']} functions had no docstring or leading comment and were "
                f"embedded with text-embedding-3-large. Call enrich_summaries('{project_id}') to "
                f"generate LLM summaries and re-embed them with {{}}, which will improve semantic search quality."
            ).format(self._pipeline.model)
        coverage = await self._verify_coverage(project_id, project_db=pdb, project_pipeline=ppipe)
        result["coverage"] = coverage.as_dict()
        if coverage.status != "ok":
            result["status"] = coverage.status
            print(f"[indexer] coverage check: {coverage.status} — {coverage.recommendation}")
        result["dependency_fingerprint"] = fp_result  # type: ignore[assignment]

        print(f"[indexer] index_project complete: {result}")
        return result

    async def _finalize_index(
        self,
        *,
        project_id: str,
        project_root: str,
        branch_override: str = "",
        ids_to_embed: set[str],
        changed_fn_ids: list[str],
        existing_summaries: dict[str, str],
        contents: dict[str, str],
        always_save_fingerprint: bool = True,
        capture_fingerprint: bool = True,
        project_db=None,
        project_pipeline=None,
    ) -> tuple[dict, dict | None]:
        """Shared post-reconciliation phases for index_project and index_changes.

        1. Embed chunks for changed function IDs.
        2. Update project record + record branch changes.
        3. Optionally capture dependency fingerprint.

        project_db: project-scoped CallGraphDB (search_path set to project schema).
                    Falls back to self._db if not provided (pre-routing callers).
        project_pipeline: matching EmbeddingPipeline. Falls back to self._pipeline.

        Returns (embed_stats, fingerprint_result).
        fingerprint_result is None when capture_fingerprint=False.
        """
        pdb = project_db or self._db
        ppipe = project_pipeline or self._pipeline

        # Phase 1: Embed
        chunks = [
            c for fp, content in contents.items()
            for c in extract_chunks(fp, content, project_root=project_root)
            if c.id in ids_to_embed
        ]
        embed_stats = {"docs": 0, "fallback": 0}
        if chunks:
            print(f"[indexer] starting embedding for {len(chunks)} chunks")
            embed_stats = await ppipe.upsert_chunks(
                chunks,
                project_id=project_id,
                existing_summaries=existing_summaries or None,
            )

        # Phase 2: Update project record + branch
        ctx = detect_branch(project_root)
        effective_branch = branch_override or ctx.branch
        head_commit = ctx.head_commit
        # upsert_project goes to the org-level DB (public.projects registry)
        await self._db.upsert_project(
            project_id, project_id, project_root,
            branch=effective_branch, head_commit=head_commit,
        )
        await pdb.record_branch_changes(
            project_id, effective_branch, changed_fn_ids, head_commit
        )

        # Phase 3: Fingerprint
        if not capture_fingerprint:
            return embed_stats, None

        deps = await pdb.list_external_dependencies(project_id)
        prev_row = await pdb.get_latest_dependency_fingerprint(project_id)
        prev_fp = (
            DependencyFingerprint.from_dict(json.loads(prev_row["snapshot_json"]))
            if prev_row else None
        )
        fp = _fingerprinter.compute(project_id, deps, project_path=project_root)
        fp_result: dict = {
            "hash": fp.fingerprint_hash,
            "libraries": fp.total_libraries,
            "symbols": fp.total_external_symbols,
        }
        diff = _fingerprinter.diff(prev_fp, fp) if prev_fp else None
        if always_save_fingerprint or prev_fp is None or fp.fingerprint_hash != prev_fp.fingerprint_hash:
            await pdb.save_dependency_fingerprint(
                project_id, uuid.uuid4().hex, fp.captured_at, fp.fingerprint_hash,
                json.dumps(fp.to_dict()),
                json.dumps(diff.to_dict()) if diff else None,
            )
        if diff and (diff.removed_symbols or diff.changed_symbols):
            fp_result["warning"] = (
                f"{len(diff.removed_symbols)} symbols removed, "
                f"{len(diff.changed_symbols)} signatures changed since last index"
            )
        return embed_stats, fp_result

    async def _verify_coverage(
        self, project_id: str, project_db=None, project_pipeline=None
    ) -> IndexCoverage:
        """
        Post-commit audit: compare all call graph nodes against all embedding vectors.
        Runs after every index_project to surface gaps before the caller sees the result.
        Three SQL queries — negligible cost relative to parsing and embedding.
        """
        pdb = project_db or self._db
        ppipe = project_pipeline or self._pipeline

        all_node_ids = await pdb.get_all_node_ids(project_id)
        embedded_ids = await ppipe.get_embedded_ids(project_id)

        missing = sorted(all_node_ids - embedded_ids)
        actual = len(embedded_ids & all_node_ids)

        degraded = await pdb.get_nodes_with_null_content(project_id)
        large_model = await pdb.count_nodes_by_model(
            project_id, "text-embedding-3-large"
        )

        return IndexCoverage(
            project_id=project_id,
            expected=len(all_node_ids),
            actual=actual,
            missing_vectors=missing,
            degraded_count=len(degraded),
            on_large_model=large_model,
        )

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

        schema_name = derive_schema_name(project_id)
        await self._db.create_project_schema(schema_name)
        pdb = await self._db.project_db(schema_name)
        ppipe = self._pipeline.with_db(pdb)

        # Snapshot existing summaries and body hashes before any deletion.
        existing_summaries: dict[str, str] = {}
        old_hashes: dict[str, str] = {}
        for fp in file_paths:
            for node in await pdb.get_nodes_by_file(fp, project_id):
                if node["summary"]:
                    existing_summaries[node["id"]] = node["summary"]
                old_hashes[node["id"]] = node.get("body_hash", "")

        updated_nodes = []
        updated_edges = []
        changed_ids: set[str] = set()
        processed_contents: dict[str, str] = {}

        print(f"[indexer] index_changes: project={project_id!r} {len(file_paths)} files")
        for fp in file_paths:
            content = file_contents.get(fp)

            if content is None:
                # Deleted file — wipe all its embeddings (while nodes still exist for ID lookup).
                await ppipe.delete_by_file(fp, project_id)
                await pdb.delete_file_data(fp, project_id)
                print(f"[indexer]   {fp}: deleted (purged from index)")
                continue

            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                await ppipe.delete_by_file(fp, project_id)
                await pdb.delete_file_data(fp, project_id)
                print(f"[indexer]   {fp}: skipped (unsupported extension {ext!r})")
                continue

            processed_contents[fp] = content
            nodes, edges = _parser.parse_file(fp, content, project_root=project_root)
            new_hashes = {n.id: n.body_hash for n in nodes}

            # Reconcile this file's old DB state against freshly parsed nodes.
            file_old_hashes = {
                n["id"]: n.get("body_hash", "")
                for n in await pdb.get_nodes_by_file(fp, project_id)
            }
            file_delta = reconcile(file_old_hashes, new_hashes)
            await ppipe.delete_by_ids(list(file_delta.to_delete), project_id)

            # Clear stale Claude summaries for functions whose body changed.
            for fn_id in file_delta.to_embed:
                existing_summaries.pop(fn_id, None)

            # Refresh call graph for the whole file — edges can change in any function.
            await pdb.delete_file_data(fp, project_id)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)
            changed_ids |= file_delta.to_embed
            print(f"[indexer]   {fp}: {len(nodes)} nodes, "
                  f"+{len(file_delta.functions_added)} new, "
                  f"~{len(file_delta.functions_changed)} changed, "
                  f"-{len(file_delta.functions_removed)} removed, "
                  f"{len(file_delta.unchanged)} unchanged")

        if updated_nodes:
            print(f"[indexer] call graph: {len(updated_nodes)} nodes, {len(updated_edges)} edges — writing to db")
            await pdb.upsert_nodes(updated_nodes, project_id)
        if updated_edges:
            all_ids = await pdb.get_all_node_ids(project_id)
            await pdb.upsert_edges(updated_edges, all_ids, project_id)
        if updated_nodes or updated_edges:
            print(f"[indexer] call graph written ok")

        embed_stats = {"docs": 0, "fallback": 0}
        if updated_nodes or changed_ids:
            embed_stats, _ = await self._finalize_index(
                project_id=project_id,
                project_root=project_root,
                branch_override="",
                ids_to_embed=changed_ids,
                changed_fn_ids=list(changed_ids),
                existing_summaries=existing_summaries,
                contents=processed_contents,
                always_save_fingerprint=False,
                capture_fingerprint=bool(updated_nodes),
                project_db=pdb,
                project_pipeline=ppipe,
            )

        result = {
            "status": "ok",
            "project_id": project_id,
            "files_updated": len([fp for fp in file_paths if file_contents.get(fp) is not None]),
            "functions_updated": len(updated_nodes),
            "functions_reembedded": len(changed_ids),
            "function_ids": [n.id for n in updated_nodes],
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
                await self._pipeline.delete_by_file(fp, project_id)
            await self._db.delete_file_data(fp, project_id)
            if content is None:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            nodes, edges = _parser.parse_file(fp, content, project_root=project_root)
            updated_nodes.extend(nodes)
            updated_edges.extend(edges)

        existing_hashes = {nid: "" for nid in existing_node_ids}
        new_hashes = {n.id: "" for n in updated_nodes}
        cg_delta = reconcile(existing_hashes, new_hashes)
        if cg_delta.functions_removed:
            await self._pipeline.delete_by_ids(list(cg_delta.functions_removed), project_id)

        if updated_nodes:
            await self._db.upsert_nodes(updated_nodes, project_id)
            new_node_ids = {n.id for n in updated_nodes}
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
            await self._pipeline.delete_by_file(fp, project_id)
            if content is None:
                continue
            ext = Path(fp).suffix.lower()
            if ext not in _SUPPORTED_EXTENSIONS:
                continue
            updated_chunks.extend(extract_chunks(fp, content, project_root=project_root))

        if updated_chunks:
            existing = {} if force_summaries else await self._pipeline.get_summaries(
                [c.id for c in updated_chunks], project_id
            )
            await self._pipeline.upsert_chunks(
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
        await self._pipeline.delete_by_ids([n["id"] for n in all_nodes], project_id)

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
        embed_stats = await self._pipeline.upsert_chunks(
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
        """Ingest an LSIF NDJSON index file into Scopenos.

        Extracts symbol definitions with their hover documentation and imports
        them as FunctionNode records into the call-graph + embedding pipeline.
        Call-edge resolution is deferred to a future version.

        path: filesystem path to the .lsif file on the Scopenos server.
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
            embed_stats = await self._pipeline.upsert_chunks(chunks, project_id=project_id)

        return {
            "status": "ok",
            "project_id": project_id,
            "symbols_imported": len(nodes),
            "edges_imported": len(edges),
            "embedded_with_docs": embed_stats["docs"],
            "embedded_large_fallback": embed_stats["fallback"],
        }

    async def index_scip(self, path: str, project_id: str = "") -> dict:
        """Ingest a SCIP JSON index file into Scopenos.

        SCIP (Sourcegraph Code Intelligence Protocol) provides structured symbol
        information with explicit documentation and relationships.  Produces
        FunctionNode records and basic call edges from relationship data.

        path: filesystem path to the .scip.json file on the Scopenos server.
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
            embed_stats = await self._pipeline.upsert_chunks(chunks, project_id=project_id)

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
        # Phase 2 (Python/TypeScript): already supported
        "python":     ["scip-python", "index", "--project-name", project_id, "."],
        "typescript": ["scip-typescript", "index", "--infer-tsconfig"],
        "javascript": ["scip-typescript", "index", "--infer-tsconfig"],
        # Phase 3 (typed compiled languages): type-resolved call graphs via SCIP.
        # These indexers must be installed separately; Scopenos silently skips
        # SCIP augmentation when the binary is absent (tree-sitter remains the source).
        "go":     ["scip-go", "--output", "index.scip"],
        "java":   ["scip-java", "index"],
        "rust":   ["rust-analyzer", "scip", "."],
        "csharp": ["scip-dotnet", "index"],
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

    # scip-python requires a git repo. If one doesn't exist (e.g. Docker image
    # copy without .git), init a temporary one so scip-python can discover files.
    git_dir = os.path.join(project_path, ".git")
    _git_inited = False
    if not os.path.exists(git_dir):
        try:
            for git_cmd in [
                ["git", "init"],
                ["git", "config", "user.email", "scip@scopenos.dev"],
                ["git", "config", "user.name", "scopenos"],
                ["git", "add", "-A"],
                ["git", "commit", "-m", "index", "--allow-empty"],
            ]:
                proc = await asyncio.create_subprocess_exec(
                    *git_cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    cwd=project_path,
                )
                await proc.wait()
            _git_inited = True
        except Exception:
            pass

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            scip_out = os.path.join(tmpdir, "index.scip.json")
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=project_path,
                )
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
                if proc.returncode != 0:
                    print(f"[indexer] scip indexer exited {proc.returncode}: {stderr.decode()[:200]}")
                    return None
            except asyncio.TimeoutError:
                print("[indexer] scip indexer timed out (>300s) — skipping SCIP for this project")
                return None
            except Exception as exc:
                print(f"[indexer] scip indexer failed: {exc}")
                return None

            # scip-python outputs index.scip; ScipImporter reads the JSON form.
            # scip-python v0.6+ embeds the converter — the output is index.scip (binary).
            # Try the bundled convert command first; fall back to direct binary read.
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
                    if conv.returncode == 0:
                        os.unlink(scip_bin)
                except Exception:
                    pass

            if not os.path.exists(scip_out):
                # JSON conversion not available — pass binary directly to ScipImporter
                if os.path.exists(scip_bin):
                    scip_out = scip_bin
                else:
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
    finally:
        # Remove the temporary git repo if we created it
        if _git_inited and os.path.exists(git_dir):
            import shutil
            shutil.rmtree(git_dir, ignore_errors=True)


def _detect_primary_language(source_files: list[str]) -> str:
    """Return the dominant language in a project based on file extension counts."""
    from collections import Counter
    counts: Counter = Counter(Path(f).suffix.lower() for f in source_files)
    # Priority order: languages with SCIP support first (enables augmentation),
    # then precision tree-sitter languages, then generic-fallback languages.
    priority = [
        ".py", ".ts", ".tsx", ".js", ".jsx",
        ".java", ".rs", ".go", ".cs",           # SCIP-enabled compiled languages
        ".cpp", ".rb",
        ".swift", ".kt", ".kts",                # Phase 2 precision parsers
        ".php", ".phtml",
        ".scala", ".sh", ".lua", ".ex",         # generic fallback
    ]
    lang_map = {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".jsx": "javascript",
        ".java": "java", ".rs": "rust", ".go": "go",
        ".cs": "csharp", ".cpp": "cpp", ".rb": "ruby",
        ".swift": "swift", ".kt": "kotlin", ".kts": "kotlin",
        ".php": "php", ".phtml": "php",
        ".scala": "scala", ".sh": "bash", ".lua": "lua",
        ".ex": "elixir", ".exs": "elixir",
    }
    for ext in priority:
        if counts.get(ext, 0) > 0:
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
