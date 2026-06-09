from __future__ import annotations

import hashlib
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
    body_hash: str = ""  # sha256[:16] of full function text — used to skip re-embedding unchanged functions
    decorators: list = field(default_factory=list)  # decorator call names, e.g. ["router.get", "login_required"]


@dataclass
class CallEdge:
    caller_id: str
    callee_name: str  # unresolved bare name; resolver in storage layer
    edge_type: str    # "calls" | "imports" | "inherits"
    file: str


class TreeSitterParser:
    def __init__(self) -> None:
        """Initialize language parsers for all installed tree-sitter grammars."""
        self._parsers: dict[str, "Parser"] = {}
        if _HAS_TREE_SITTER:
            self._parsers[".py"] = Parser(Language(tspython.language()))
        if _HAS_TREE_SITTER and _HAS_TS:
            self._parsers[".ts"] = Parser(Language(tstypescript.language_typescript()))
            self._parsers[".tsx"] = Parser(Language(tstypescript.language_tsx()))
            # JS/JSX use the TypeScript grammar — it's a strict superset of JavaScript
            self._parsers[".js"] = Parser(Language(tstypescript.language_typescript()))
            self._parsers[".jsx"] = Parser(Language(tstypescript.language_tsx()))

    @property
    def supported_extensions(self) -> set[str]:
        """Return the set of file extensions this parser can handle."""
        return set(self._parsers.keys())

    def parse_file(
        self, file_path: str, content: str, project_root: str = ""
    ) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Parse a source file and return (nodes, edges) for all functions and calls."""
        ext = Path(file_path).suffix.lower()
        if ext not in self._parsers:
            return [], []

        source = content.encode("utf-8", errors="replace")
        tree = self._parsers[ext].parse(source)
        module = _path_to_module(file_path, project_root)

        if ext == ".py":
            return _parse_python(tree.root_node, file_path, module, source)
        if ext in (".ts", ".tsx", ".js", ".jsx"):
            return _parse_typescript(tree.root_node, file_path, module, source)
        return [], []


# ── Python ────────────────────────────────────────────────────────────────────

def _parse_python(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Entry point for Python AST traversal — collects all nodes and call edges."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_python(root, file_path, module, source, nodes, edges,
                  parent_class=None, enclosing_func=None, enclosing_class=None)
    # Add import edges from top-level import statements
    _extract_python_imports(root, file_path, module, source, edges)
    return nodes, edges


def _extract_decorator_name(dec_node: "Node", source: bytes) -> str:
    """Return the callable name from a decorator node, without arguments.

    @router.get("/path")  →  "router.get"
    @login_required       →  "login_required"
    @app.route("/", methods=["GET"])  →  "app.route"
    """
    for child in dec_node.children:
        if child.type in ("identifier", "attribute"):
            return _text(child, source)
        if child.type == "call":
            func_part = next(
                (c for c in child.children if c.type in ("identifier", "attribute")),
                None,
            )
            if func_part:
                return _text(func_part, source)
    return ""


def _visit_python(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
    enclosing_func: str | None = None,
    enclosing_class: str | None = None,
    _decorators: list | None = None,
) -> None:
    """Recursively visit a Python AST node, extracting FunctionNodes and CallEdges."""
    # enclosing_func: full ID of the innermost enclosing function (None at module scope)
    # enclosing_class: full ID of the innermost enclosing class (None at module/function scope)
    # parent_class: bare class name — truthy when directly inside a class body
    # _decorators: propagated from a parent decorated_definition node

    if node.type == "decorated_definition":
        # Collect all @decorator names, then recurse into the inner definition
        # with those names attached — so function/class nodes get their decorators.
        decs: list[str] = []
        inner = None
        for child in node.children:
            if child.type == "decorator":
                name = _extract_decorator_name(child, source)
                if name:
                    decs.append(name)
            elif child.type in ("function_definition", "class_definition"):
                inner = child
        if inner:
            _visit_python(inner, file_path, module, source, nodes, edges,
                          parent_class=parent_class,
                          enclosing_func=enclosing_func,
                          enclosing_class=enclosing_class,
                          _decorators=decs)
        return

    if node.type == "class_definition":
        class_name = _child_text(node, "identifier", source)
        if class_name:
            # Scope class_id to its full qualified location:
            #   inside a function   → <enclosing_func>.<locals>.<class>
            #   inside a class      → <enclosing_class>.<class>
            #   at module scope     → <module>.<class>
            if enclosing_func:
                class_id = f"{enclosing_func}.<locals>.{class_name}"
            elif enclosing_class:
                class_id = f"{enclosing_class}.{class_name}"
            else:
                class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            # Inheritance edges
            bases_node = next((c for c in node.children if c.type == "argument_list"), None)
            if bases_node:
                for base in bases_node.children:
                    if base.type == "identifier":
                        edges.append(CallEdge(caller_id=class_id, callee_name=_text(base, source), edge_type="inherits", file=file_path))
            nodes.append(FunctionNode(id=class_id, name=class_name, file=file_path, module=module, type="class", signature=sig, body="", docstring=""))
            # Recurse into class body: class_id becomes the new enclosing_class.
            # enclosing_func is preserved so nested functions/methods inside this class
            # can still reference the correct function scope.
            for child in node.children:
                _visit_python(child, file_path, module, source, nodes, edges,
                              parent_class=class_name,
                              enclosing_func=enclosing_func,
                              enclosing_class=class_id)
        return

    if node.type == "function_definition":
        func_name_raw = _child_text(node, "identifier", source)
        if not func_name_raw:
            return
        qual_name = f"{parent_class}.{func_name_raw}" if parent_class else func_name_raw
        # Compute func_id based on the enclosing scope:
        if parent_class:
            # Class method: use enclosing_class for the full path (falls back to module.qual_name).
            if enclosing_class:
                func_id = f"{enclosing_class}.{func_name_raw}"
            else:
                func_id = f"{module}.{qual_name}"
        elif enclosing_func:
            # Nested function inside a function.
            func_id = f"{enclosing_func}.<locals>.{func_name_raw}"
        else:
            # Top-level function.
            func_id = f"{module}.{func_name_raw}"
        func_text = _node_text(node, source)
        signature = func_text.split("\n")[0].strip()
        docstring = _extract_python_docstring(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]

        nodes.append(FunctionNode(
            id=func_id, name=qual_name, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=signature, body=func_text[:2000], docstring=docstring,
            body_hash=body_hash,
            decorators=_decorators or [],
        ))

        # Collect calls inside this function (not descending into nested defs)
        _collect_python_calls(node, func_id, file_path, source, edges)
        # Recurse for nested defs: func_id becomes the new enclosing_func, class scope is reset.
        for child in node.children:
            if child.type == "block":
                for stmt in child.children:
                    if stmt.type in ("function_definition", "class_definition"):
                        _visit_python(stmt, file_path, module, source, nodes, edges,
                                      parent_class=parent_class,
                                      enclosing_func=func_id,
                                      enclosing_class=None)
        return

    for child in node.children:
        _visit_python(child, file_path, module, source, nodes, edges,
                      parent_class=parent_class, enclosing_func=enclosing_func,
                      enclosing_class=enclosing_class)


def _collect_python_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Walk a subtree collecting call edges, stopping at nested function/class definitions."""
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
    """Add import edges for all import/import-from statements in the module root."""
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
    """Extract and return the docstring text from a function_definition node, if present."""
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


def _extract_ts_jsdoc(node: "Node", source: bytes) -> str:
    """Extract the JSDoc block (/** ... */) immediately preceding a TypeScript node.

    Scans backwards in the source bytes from the node's start position.
    Returns empty string if no JSDoc is present or if non-whitespace separates
    the comment from the node (meaning it belongs to a different construct).
    """
    before = source[:node.start_byte]
    end_idx = before.rfind(b"*/")
    if end_idx < 0:
        return ""
    start_idx = before.rfind(b"/**", 0, end_idx + 2)
    if start_idx < 0:
        return ""
    # Reject if anything other than whitespace sits between comment and node
    if before[end_idx + 2:].strip():
        return ""
    content = before[start_idx + 3:end_idx].decode("utf-8", errors="replace")
    lines = [line.strip().lstrip("* ") for line in content.splitlines()]
    return " ".join(l for l in lines if l).strip()[:500]

_TS_FUNC_TYPES = {
    "function_declaration",
    "method_definition",
    "arrow_function",
    "function_expression",
}


def _parse_typescript(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Entry point for TypeScript AST traversal — collects all nodes and call edges."""
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
    """Recursively visit a TypeScript AST node, extracting FunctionNodes and CallEdges."""
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
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        docstring = _extract_ts_jsdoc(node, source)

        nodes.append(FunctionNode(
            id=func_id, name=qual_name, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=signature, body=func_text[:2000], docstring=docstring,
            body_hash=body_hash,
        ))

        _collect_ts_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_typescript(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_ts_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Walk a TypeScript subtree collecting call expression edges, skipping nested functions."""
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
    """Decode the source bytes for a tree-sitter node to a UTF-8 string."""
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _node_text(node: "Node", source: bytes) -> str:
    """Return the full source text of a tree-sitter node."""
    return _text(node, source)


def _child_text(node: "Node", child_type: str, source: bytes) -> str:
    """Return the text of the first child with the given node type, or empty string."""
    child = next((c for c in node.children if c.type == child_type), None)
    return _text(child, source) if child else ""


def _resolve_call_name(node: "Node", source: bytes) -> str:
    """Extract the rightmost identifier from a call-expression function reference node."""
    if node.type == "identifier":
        return _text(node, source)
    # attribute / member_expression: take the rightmost identifier
    children = list(node.children)
    for child in reversed(children):
        if child.type in ("identifier", "property_identifier"):
            return _text(child, source)
    return ""


def _walk(node: "Node"):
    """Yield every node in a tree-sitter subtree in pre-order."""
    yield node
    for child in node.children:
        yield from _walk(child)


def _path_to_module(file_path: str, project_root: str) -> str:
    """Convert a source file path to a dotted module name, relative to project_root."""
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
