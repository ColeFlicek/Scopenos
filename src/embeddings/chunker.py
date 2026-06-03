from __future__ import annotations

from dataclasses import dataclass

from ..call_graph.parser import FunctionNode, TreeSitterParser

# Reuse the tree-sitter parser; chunker is a thin wrapper that enriches output
_parser = TreeSitterParser()


@dataclass
class FunctionChunk:
    id: str
    name: str
    file: str
    module: str
    type: str
    signature: str
    docstring: str
    body: str           # full body, not truncated here — embedder truncates to token budget
    embed_text: str     # pre-formatted text to embed (populated by prepare_embed_text)
    summary: str = ""   # populated later by LLM


def extract_chunks(file_path: str, content: str, project_root: str = "") -> list[FunctionChunk]:
    nodes, _ = _parser.parse_file(file_path, content, project_root)
    return [_node_to_chunk(n) for n in nodes]


def _node_to_chunk(node: FunctionNode) -> FunctionChunk:
    chunk = FunctionChunk(
        id=node.id,
        name=node.name,
        file=node.file,
        module=node.module,
        type=node.type,
        signature=node.signature,
        docstring=node.docstring,
        body=node.body,
        embed_text="",
    )
    return chunk


def prepare_embed_text(chunk: FunctionChunk) -> str:
    """
    Format the text that gets embedded.
    Order: signature → docstring → summary → body (truncated to ~512 tokens ≈ 2000 chars).
    """
    parts = [f"Function: {chunk.id}", f"Signature: {chunk.signature}"]
    if chunk.docstring:
        parts.append(f"Docstring: {chunk.docstring}")
    if chunk.summary:
        parts.append(f"Summary: {chunk.summary}")
    body_truncated = chunk.body[:2000] if chunk.body else ""
    if body_truncated:
        parts.append(f"Body:\n{body_truncated}")
    return "\n".join(parts)
