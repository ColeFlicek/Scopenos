"""
SCIP (Sourcegraph Code Intelligence Protocol) JSON ingestion for ACIP.

SCIP is a protobuf-based format produced by indexers like scip-python,
scip-typescript, rust-analyzer, and scip-java.  The JSON form (produced
by `scip convert --to json` or directly by some indexers) is what we
consume here.

SCIP is structurally cleaner than LSIF for our purposes: each document
has a flat `symbols` list with explicit documentation and `relationships`,
making symbol extraction straightforward.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .call_graph.parser import CallEdge, FunctionNode


class ScipImporter:
    """Parse a SCIP JSON file and return FunctionNode / CallEdge records."""

    def __init__(self, project_root: str = "") -> None:
        """Store the project root used to resolve relative document paths."""
        self._root = project_root.rstrip("/")

    # ── Public API ────────────────────────────────────────────────────────────

    def parse(self, source: str) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Accept a filesystem path to a SCIP JSON file or raw JSON content."""
        try:
            p = Path(source)
            if p.exists() and p.is_file():
                content = p.read_text(encoding="utf-8")
            else:
                content = source
        except (OSError, ValueError):
            content = source

        try:
            data = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid SCIP JSON: {exc}") from exc

        return self._extract(data)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _extract(
        self, data: dict
    ) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Walk the SCIP document list and produce FunctionNode / CallEdge records."""
        nodes: list[FunctionNode] = []
        edges: list[CallEdge] = []
        seen: set[str] = set()

        for doc in data.get("documents", []):
            rel_path = doc.get("relativePath", "")
            file_path = (
                str(Path(self._root) / rel_path) if self._root else rel_path
            )
            module = _path_to_module(rel_path)

            for sym in doc.get("symbols", []):
                sym_id = sym.get("symbol", "")
                if not sym_id:
                    continue

                docs = sym.get("documentation", [])
                doc_text = "\n".join(docs)
                lines = [l for l in doc_text.splitlines() if l.strip()]
                signature = lines[0][:400] if lines else sym_id
                docstring = "\n".join(lines[1:]).strip()[:500] if len(lines) > 1 else ""

                kind = "function"
                if any(k in signature.lower() for k in ("class ", "interface ", "struct ", "trait ")):
                    kind = "class"

                name = _scip_name(sym_id)
                node_id = f"{module}.{_norm(sym_id)}"

                if node_id in seen:
                    continue
                seen.add(node_id)

                body_hash = hashlib.sha256(
                    f"{file_path}:{sym_id}".encode()
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

                # Relationship edges: isReference means this symbol references another
                for rel in sym.get("relationships", []):
                    if rel.get("isReference") or rel.get("isImplementation"):
                        callee_sym = rel.get("symbol", "")
                        if callee_sym:
                            edges.append(CallEdge(
                                caller_id=node_id,
                                callee_name=_scip_name(callee_sym),
                                edge_type="calls",
                                file=file_path,
                            ))

        return nodes, edges


# ── Helpers ───────────────────────────────────────────────────────────────────

def _scip_name(symbol: str) -> str:
    """Extract the bare identifier from a SCIP symbol string.

    SCIP symbols look like:
      scip-python python package 1.0.0 src/`foo.py`:MyClass#method().
      scip-typescript npm pkg 1.0.0 src/utils.ts/MyClass#method().
    """
    if not symbol:
        return "unknown"
    # Take the descriptor (last space-separated segment)
    parts = symbol.split(" ")
    descriptor = parts[-1] if len(parts) > 1 else symbol
    # Strip trailing punctuation and split on hierarchy separators
    descriptor = descriptor.rstrip(".")
    for sep in ("#", "/", ".", "`"):
        if sep in descriptor:
            descriptor = descriptor.rsplit(sep, 1)[-1]
    return descriptor.strip("()") or "unknown"


def _norm(name: str) -> str:
    """Normalise a SCIP symbol to a safe dotted module-path segment."""
    return (name.replace("/", ".").replace("#", ".").replace("`", "")
            .replace("(", "").replace(")", "").replace(" ", "_")
            .strip(".")) or "unknown"


def _path_to_module(file_path: str) -> str:
    """Convert a relative file path to a dotted module identifier."""
    parts = Path(file_path).with_suffix("").parts
    return ".".join(p for p in parts if p not in (".", "..")) or "unknown"
