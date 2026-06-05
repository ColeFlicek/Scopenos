from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

try:
    import tree_sitter_python as tspython
    from tree_sitter import Language, Parser, Node
    _HAS_TREE_SITTER = True
except ImportError:
    _HAS_TREE_SITTER = False

try:
    import tree_sitter_typescript as tstypescript
    _HAS_TS = True
except ImportError:
    _HAS_TS = False


@dataclass
class FunctionNode:
    id: str           # module.ClassName.method or module.func_name
    name: str         # bare name (may include ClassName prefix)
    file: str
    module: str
    type: str         # "function" | "method" | "class"
    signature: str
    body: str         # truncated at 2000 chars for storage
    docstring: str


@dataclass
class CallEdge:
    caller_id: str
    callee_name: str  # unresolved bare name; resolver in storage layer
    edge_type: str    # "calls" | "imports" | "inherits"
    file: str


class TreeSitterParser:
    def __init__(self) -> None:
        self._parsers: dict[str, "Parser"] = {}
        if _HAS_TREE_SITTER:
            self._parsers[".py"] = Parser(Language(tspython.language()))
        if _HAS_TREE_SITTER and _HAS_TS:
            self._parsers[".ts"] = Parser(Language(tstypescript.language_typescript()))
            self._parsers[".tsx"] = Parser(Language(tstypescript.language_tsx()))

    @property
    def supported_extensions(self) -> set[str]:
        return set(self._parsers.keys())

    def parse_file(
        self, file_path: str, content: str, project_root: str = ""
    ) -> tuple[list[FunctionNode], list[CallEdge]]:
        ext = Path(file_path).suffix.lower()
        if ext not in self._parsers:
            return [], []

        source = content.encode("utf-8", errors="replace")
        tree = self._parsers[ext].parse(source)
        module = _path_to_module(file_path, project_root)

        if ext == ".py":
            return _parse_python(tree.root_node, file_path, module, source)
        if ext in (".ts", ".tsx"):
            return _parse_typescript(tree.root_node, file_path, module, source)
        return [], []


# ── Python ────────────────────────────────────────────────────────────────────

def _parse_python(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_python(root, file_path, module, source, nodes, edges, parent_class=None, enclosing_func=None)
    # Add import edges from top-level import statements
    _extract_python_imports(root, file_path, module, source, edges)
    return nodes, edges


def _visit_python(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
    enclosing_func: str | None = None,
) -> None:
    if node.type == "class_definition":
        class_name = _child_text(node, "identifier", source)
        # Emit a class node
        if class_name:
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            # Inheritance edges
            bases_node = next((c for c in node.children if c.type == "argument_list"), None)
            if bases_node:
                for base in bases_node.children:
                    if base.type == "identifier":
                        edges.append(CallEdge(caller_id=class_id, callee_name=_text(base, source), edge_type="inherits", file=file_path))
            nodes.append(FunctionNode(id=class_id, name=class_name, file=file_path, module=module, type="class", signature=sig, body="", docstring=""))
            # Recurse into class body with class context (reset enclosing_func — class scope is not a function scope)
            for child in node.children:
                _visit_python(child, file_path, module, source, nodes, edges, parent_class=class_name, enclosing_func=None)
        return

    if node.type == "function_definition":
        func_name_raw = _child_text(node, "identifier", source)
        if not func_name_raw:
            return
        qual_name = f"{parent_class}.{func_name_raw}" if parent_class else func_name_raw
        # Use <locals> convention to disambiguate nested functions with the same name
        # that appear inside different parent functions (e.g. two functions both containing def _helper).
        if enclosing_func:
            func_id = f"{module}.{enclosing_func}.<locals>.{qual_name}"
        else:
            func_id = f"{module}.{qual_name}"
        func_text = _node_text(node, source)
        signature = func_text.split("\n")[0].strip()
        docstring = _extract_python_docstring(node, source)
        body_text = func_text[:2000]

        nodes.append(FunctionNode(
            id=func_id, name=qual_name, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=signature, body=body_text, docstring=docstring,
        ))

        # Collect calls inside this function (not descending into nested defs)
        _collect_python_calls(node, func_id, file_path, source, edges)
        # Recurse for nested defs, passing this function as the enclosing scope
        for child in node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type in ("function_definition", "class_definition"):
                        _visit_python(stmt, file_path, module, source, nodes, edges,
                                      parent_class=parent_class, enclosing_func=func_id)
        return

    for child in node.children:
        _visit_python(child, file_path, module, source, nodes, edges,
                      parent_class=parent_class, enclosing_func=enclosing_func)


def _collect_python_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Recursively collect call nodes, stopping at nested function/class definitions."""
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            continue  # don't recurse into nested defs or classes
        if child.type == "call":
            func_part = next((c for c in child.children if c.type in ("identifier", "attribute")), None)
            if func_part:
                name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(caller_id=caller_id, callee_name=name, edge_type="calls", file=file_path))
        _collect_python_calls(child, caller_id, file_path, source, edges)


def _extract_python_imports(
    root: "Node", file_path: str, module: str, source: bytes, edges: list[CallEdge]
) -> None:
    for node in _walk(root):
        if node.type == "import_statement":
            for child in node.children:
                if child.type == "dotted_name":
                    edges.append(CallEdge(caller_id=module, callee_name=_text(child, source), edge_type="imports", file=file_path))
        elif node.type == "import_from_statement":
            module_node = next((c for c in node.children if c.type == "dotted_name"), None)
            if module_node:
                edges.append(CallEdge(caller_id=module, callee_name=_text(module_node, source), edge_type="imports", file=file_path))


def _extract_python_docstring(func_node: "Node", source: bytes) -> str:
    block = next((c for c in func_node.children if c.type == "block"), None)
    if not block:
        return ""
    first_stmt = next((c for c in block.children if c.type not in ("\n", "comment")), None)
    if not first_stmt or first_stmt.type != "expression_statement":
        return ""
    string_node = next((c for c in first_stmt.children if c.type == "string"), None)
    if not string_node:
        return ""
    raw = _text(string_node, source)
    # Skip string prefix chars (r, b, u, f and combinations) before stripping delimiters.
    i = 0
    while i < len(raw) and raw[i] in "rRbBuUfF":
        i += 1
    raw = raw[i:]
    # Strip the matching delimiter pair (triple before single to avoid partial strips).
    for delim in ('"""', "'''", '"', "'"):
        if raw.startswith(delim) and raw.endswith(delim) and len(raw) >= 2 * len(delim):
            return raw[len(delim):-len(delim)][:500]
    return raw[:500]


# ── TypeScript ────────────────────────────────────────────────────────────────

_TS_FUNC_TYPES = {
    "function_declaration",
    "method_definition",
    "arrow_function",
    "function_expression",
}


def _parse_typescript(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_typescript(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_typescript(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    if node.type == "class_declaration":
        class_name = _child_text(node, "type_identifier", source) or _child_text(node, "identifier", source)
        if class_name:
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            # heritage (extends)
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)
            if heritage:
                for h in heritage.children:
                    if h.type == "identifier":
                        edges.append(CallEdge(caller_id=class_id, callee_name=_text(h, source), edge_type="inherits", file=file_path))
            nodes.append(FunctionNode(id=class_id, name=class_name, file=file_path, module=module, type="class", signature=sig, body="", docstring=""))
            for child in node.children:
                _visit_typescript(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type in _TS_FUNC_TYPES:
        func_name_raw = (
            _child_text(node, "property_identifier", source)
            or _child_text(node, "identifier", source)
        )
        if not func_name_raw:
            for child in node.children:
                _visit_typescript(child, file_path, module, source, nodes, edges, parent_class=parent_class)
            return

        qual_name = f"{parent_class}.{func_name_raw}" if parent_class else func_name_raw
        func_id = f"{module}.{qual_name}"
        func_text = _node_text(node, source)
        signature = func_text.split("\n")[0].strip()

        nodes.append(FunctionNode(
            id=func_id, name=qual_name, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=signature, body=func_text[:2000], docstring="",
        ))

        _collect_ts_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_typescript(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_ts_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    for child in node.children:
        if child.type in _TS_FUNC_TYPES:
            continue
        if child.type == "call_expression":
            func_part = next((c for c in child.children if c.type in ("identifier", "member_expression")), None)
            if func_part:
                name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(caller_id=caller_id, callee_name=name, edge_type="calls", file=file_path))
        _collect_ts_calls(child, caller_id, file_path, source, edges)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _text(node: "Node", source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _node_text(node: "Node", source: bytes) -> str:
    return _text(node, source)


def _child_text(node: "Node", child_type: str, source: bytes) -> str:
    child = next((c for c in node.children if c.type == child_type), None)
    return _text(child, source) if child else ""


def _resolve_call_name(node: "Node", source: bytes) -> str:
    if node.type == "identifier":
        return _text(node, source)
    # attribute / member_expression: take the rightmost identifier
    children = list(node.children)
    for child in reversed(children):
        if child.type in ("identifier", "property_identifier"):
            return _text(child, source)
    return ""


def _walk(node: "Node"):
    yield node
    for child in node.children:
        yield from _walk(child)


def _path_to_module(file_path: str, project_root: str) -> str:
    if project_root:
        try:
            rel = os.path.relpath(file_path, project_root)
        except ValueError:
            rel = file_path
    else:
        rel = os.path.basename(file_path)

    rel = rel.replace("\\", "/")
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx"):
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    return rel.replace("/", ".").strip(".")
