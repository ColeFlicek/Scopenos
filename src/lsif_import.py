"""
LSIF (Language Server Index Format) ingestion for Phronosis.

LSIF is an NDJSON format where each line is a JSON vertex or edge produced
by tools like lsif-py, lsif-tsc, lsif-java, rust-analyzer, etc.  We build
an in-memory graph from the LSIF objects, then extract function/class
definitions with their hover documentation and import them into Phronosis's
call-graph + embedding pipeline.

Call-edge resolution is deferred to v2 — the LSIF reference graph does not
directly encode enclosing-function containment, so accurately mapping
references to call edges requires additional heuristics.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .call_graph.parser import CallEdge, FunctionNode


class LsifImporter:
    """Parse an LSIF NDJSON dump and return FunctionNode / CallEdge records."""

    def __init__(self, project_root: str = "") -> None:
        """Store the project root used to strip URI prefixes from document paths."""
        self._root = project_root.rstrip("/")

    # ── Public API ────────────────────────────────────────────────────────────

    def parse(self, source: str) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Accept a filesystem path to an LSIF file or raw NDJSON content."""
        try:
            p = Path(source)
            if p.exists() and p.is_file():
                content = p.read_text(encoding="utf-8")
            else:
                content = source
        except (OSError, ValueError):
            content = source
        return self._parse_content(content)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _parse_content(
        self, content: str
    ) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Parse NDJSON lines into vertex/edge maps then extract nodes."""
        vertices: dict[int, dict] = {}
        edges: list[dict] = []

        for line in content.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "vertex":
                vertices[obj["id"]] = obj
            elif obj.get("type") == "edge":
                edges.append(obj)

        return self._extract(vertices, edges)

    def _extract(
        self, vertices: dict[int, dict], edges: list[dict]
    ) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Build adjacency maps and produce FunctionNode records from definition ranges."""

        # ── Adjacency maps ────────────────────────────────────────────────────
        range_to_rs: dict[int, int] = {}         # range  → resultSet
        rs_to_hover: dict[int, int] = {}          # resultSet → hoverResult
        rs_to_def: dict[int, int] = {}            # resultSet → definitionResult
        def_to_ranges: dict[int, list[int]] = {}  # definitionResult → [ranges]
        range_to_mk: dict[int, int] = {}          # range → moniker
        rs_to_mk: dict[int, int] = {}             # resultSet → moniker
        doc_to_ranges: dict[int, list[int]] = {}  # document → [ranges]

        for e in edges:
            lbl = e.get("label", "")
            out = e.get("outV", 0)
            in_v = e.get("inV")
            in_vs: list[int] = e.get("inVs") or ([] if in_v is None else [in_v])
            out_label = vertices.get(out, {}).get("label", "")

            if lbl == "next":
                range_to_rs[out] = in_v
            elif lbl == "textDocument/hover":
                rs_to_hover[out] = in_v
            elif lbl == "textDocument/definition":
                rs_to_def[out] = in_v
            elif lbl == "contains" and out_label == "document":
                doc_to_ranges.setdefault(out, []).extend(in_vs)
            elif lbl == "item" and out_label == "definitionResult":
                def_to_ranges.setdefault(out, []).extend(in_vs)
            elif lbl == "moniker":
                if out_label == "range":
                    range_to_mk[out] = in_v
                elif out_label == "resultSet":
                    rs_to_mk[out] = in_v

        # range → document (reverse of doc_to_ranges)
        range_to_doc: dict[int, int] = {}
        for doc_id, rids in doc_to_ranges.items():
            for rid in rids:
                range_to_doc[rid] = doc_id

        # All definition ranges (those referenced by a definitionResult via item edges)
        definition_ranges: set[int] = set()
        for def_id in rs_to_def.values():
            definition_ranges.update(def_to_ranges.get(def_id, []))

        # ── Extract FunctionNodes ─────────────────────────────────────────────
        nodes: list[FunctionNode] = []
        seen: set[str] = set()

        for rid in definition_ranges:
            rv = vertices.get(rid)
            if not rv or rv.get("label") != "range":
                continue

            doc_id = range_to_doc.get(rid)
            if doc_id is None:
                continue
            doc_v = vertices.get(doc_id, {})
            file_path = _uri_to_path(doc_v.get("uri", ""), self._root)
            if not file_path:
                continue

            # Hover text → signature + docstring
            rs_id = range_to_rs.get(rid)
            signature = ""
            docstring = ""
            kind = "function"
            if rs_id:
                hover_id = rs_to_hover.get(rs_id)
                if hover_id:
                    hover_text = _extract_hover(vertices.get(hover_id, {}))
                    lines = [l for l in hover_text.splitlines() if l.strip()]
                    signature = lines[0][:400] if lines else ""
                    docstring = "\n".join(lines[1:]).strip()[:500] if len(lines) > 1 else ""
                    if any(k in signature.lower() for k in ("class ", "struct ", "interface ")):
                        kind = "class"

            # Stable identifier from moniker
            mk_id = range_to_mk.get(rid) or (rs_to_mk.get(rs_id) if rs_id else None)
            symbol = ""
            if mk_id:
                symbol = vertices.get(mk_id, {}).get("identifier", "")

            line_no = rv.get("start", {}).get("line", 0)
            name = _symbol_to_name(symbol) or _name_from_sig(signature) or f"sym_L{line_no}"
            module = _path_to_module(file_path)
            node_id = f"{module}.{_norm(symbol or name)}"

            if node_id in seen:
                continue
            seen.add(node_id)

            body_hash = hashlib.sha256(
                f"{file_path}:{line_no}:{name}".encode()
            ).hexdigest()[:16]

            nodes.append(FunctionNode(
                id=node_id,
                name=name,
                file=file_path,
                module=module,
                type=kind,
                signature=signature,
                body="",
                docstring=docstring,
                body_hash=body_hash,
            ))

        # Call edges deferred to v2 (see module docstring).
        return nodes, []


# ── Helpers ───────────────────────────────────────────────────────────────────

def _uri_to_path(uri: str, root: str) -> str:
    """Strip the file:// scheme and the project root prefix from a document URI."""
    if uri.startswith("file://"):
        uri = uri[7:]
    if root and uri.startswith(root):
        uri = uri[len(root):].lstrip("/")
    return uri


def _extract_hover(vertex: dict) -> str:
    """Pull plain-text content out of a hoverResult vertex."""
    result = vertex.get("result", {})
    contents = result.get("contents", "")
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return contents.get("value", "")
    if isinstance(contents, list):
        parts = []
        for c in contents:
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, dict):
                parts.append(c.get("value", ""))
        return "\n".join(p for p in parts if p)
    return ""


def _symbol_to_name(symbol: str) -> str:
    """Extract the bare function/class name from a LSIF moniker identifier."""
    if not symbol:
        return ""
    # Strip scheme prefix like "npm pyright:" or "tsc:"
    if " " in symbol:
        symbol = symbol.rsplit(" ", 1)[-1]
    for sep in ("#", "$", ".", "/", ":"):
        if sep in symbol:
            symbol = symbol.rsplit(sep, 1)[-1]
    return symbol.strip("().")


def _name_from_sig(sig: str) -> str:
    """Extract the function/class name from the first line of a hover signature."""
    first = (sig.split("\n")[0] if "\n" in sig else sig).strip()
    for prefix in ("function ", "class ", "def ", "const ", "let ", "var ",
                   "type ", "interface ", "struct ", "fn ", "func "):
        if first.lower().startswith(prefix):
            rest = first[len(prefix):]
            return rest.split("(")[0].split("<")[0].split(" ")[0].strip()
    return ""


def _norm(name: str) -> str:
    """Normalise a symbol identifier to a safe dotted-path segment."""
    return (name.replace("/", ".").replace("#", ".").replace("$", ".")
            .replace("(", "").replace(")", "").replace(" ", "_")
            .strip(".")) or "unknown"


def _path_to_module(file_path: str) -> str:
    """Convert a relative file path to a dotted module identifier."""
    parts = Path(file_path).with_suffix("").parts
    return ".".join(p for p in parts if p not in (".", "..")) or "unknown"
