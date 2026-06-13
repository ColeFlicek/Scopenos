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

try:
    import tree_sitter_rust as tsrust
    _HAS_RUST = True
except ImportError:
    _HAS_RUST = False

try:
    import tree_sitter_go as tsgo
    _HAS_GO = True
except ImportError:
    _HAS_GO = False

try:
    import tree_sitter_java as tsjava
    _HAS_JAVA = True
except ImportError:
    _HAS_JAVA = False

try:
    import tree_sitter_cpp as tscpp
    _HAS_CPP = True
except ImportError:
    _HAS_CPP = False

try:
    import tree_sitter_c_sharp as tscsharp
    _HAS_CSHARP = True
except ImportError:
    _HAS_CSHARP = False

try:
    import tree_sitter_ruby as tsruby
    _HAS_RUBY = True
except ImportError:
    _HAS_RUBY = False


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
    leading_comment: str = ""  # leading # comment block before first real statement
    body_hash: str = ""  # sha256[:16] of full function text — used to skip re-embedding unchanged functions
    decorators: list = field(default_factory=list)  # decorator call names, e.g. ["router.get", "login_required"]
    is_external: bool = False  # True for nodes from external libraries (SCIP reference targets)


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
        if _HAS_TREE_SITTER and _HAS_RUST:
            self._parsers[".rs"] = Parser(Language(tsrust.language()))
        if _HAS_TREE_SITTER and _HAS_GO:
            self._parsers[".go"] = Parser(Language(tsgo.language()))
        if _HAS_TREE_SITTER and _HAS_JAVA:
            self._parsers[".java"] = Parser(Language(tsjava.language()))
        if _HAS_TREE_SITTER and _HAS_CPP:
            self._parsers[".cpp"] = Parser(Language(tscpp.language()))
            self._parsers[".cc"] = Parser(Language(tscpp.language()))
            self._parsers[".hpp"] = Parser(Language(tscpp.language()))
        if _HAS_TREE_SITTER and _HAS_CSHARP:
            self._parsers[".cs"] = Parser(Language(tscsharp.language()))
        if _HAS_TREE_SITTER and _HAS_RUBY:
            self._parsers[".rb"] = Parser(Language(tsruby.language()))

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
        if ext == ".rs":
            return _parse_rust(tree.root_node, file_path, module, source)
        if ext == ".go":
            return _parse_go(tree.root_node, file_path, module, source)
        if ext == ".java":
            return _parse_java(tree.root_node, file_path, module, source)
        if ext in (".cpp", ".cc", ".hpp"):
            return _parse_cpp(tree.root_node, file_path, module, source)
        if ext == ".cs":
            return _parse_csharp(tree.root_node, file_path, module, source)
        if ext == ".rb":
            return _parse_ruby(tree.root_node, file_path, module, source)
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
        leading_comment = _extract_python_leading_comment(node, source) if not docstring else ""
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]

        nodes.append(FunctionNode(
            id=func_id, name=qual_name, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=signature, body=func_text[:2000], docstring=docstring,
            leading_comment=leading_comment,
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


def _extract_python_leading_comment(func_node: "Node", source: bytes) -> str:
    """Extract consecutive # comment lines at the top of a function body (before any real code)."""
    block = next((c for c in func_node.children if c.type == "block"), None)
    if not block:
        return ""
    lines = []
    for child in block.children:
        if child.type == "\n":
            continue
        if child.type == "comment":
            lines.append(_text(child, source).lstrip("# ").strip())
        else:
            break
    return " ".join(lines)[:500]


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




# ── Rust ──────────────────────────────────────────────────────────────────────

def _extract_rust_doc(node: "Node", source: bytes) -> str:
    """Extract consecutive /// doc-comment lines immediately before a Rust node."""
    before = source[:node.start_byte]
    lines = before.decode("utf-8", errors="replace").splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("///"):
            doc_lines.insert(0, stripped[3:].strip())
        elif stripped == "" or stripped.startswith("#["):
            continue  # skip blank lines and attributes between comment and fn
        else:
            break
    return " ".join(doc_lines).strip()[:500]


_RUST_FUNC_TYPES = {"function_item"}
_RUST_CLASS_TYPES = {"struct_item", "enum_item", "trait_item"}


def _parse_rust(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Extract functions and struct/enum/trait definitions from a Rust source file."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_rust(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_rust(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    """Recursively visit a Rust AST, collecting function and type items."""
    if node.type in _RUST_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type == "type_identifier"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="",
                docstring=_extract_rust_doc(node, source), body_hash=hashlib.sha256(
                    _node_text(node, source).encode("utf-8", errors="replace")
                ).hexdigest()[:16],
            ))
        for child in node.children:
            _visit_rust(child, file_path, module, source, nodes, edges, parent_class=class_name if name_node else parent_class)
        return

    if node.type == "impl_item":
        # Extract the type name from impl declarations
        type_node = next((c for c in node.children if c.type == "type_identifier"), None)
        impl_class = _text(type_node, source) if type_node else parent_class
        for child in node.children:
            _visit_rust(child, file_path, module, source, nodes, edges, parent_class=impl_class)
        return

    if node.type in _RUST_FUNC_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if not name_node:
            return
        func_name = _text(name_node, source)
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=func_text.split("\n")[0].strip(), body=func_text[:2000],
            docstring=_extract_rust_doc(node, source), body_hash=body_hash,
        ))
        _collect_rust_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_rust(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_rust_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Collect call_expression and method_call_expression edges inside a Rust function."""
    for child in node.children:
        if child.type in _RUST_FUNC_TYPES:
            continue
        if child.type == "call_expression":
            func_part = next((c for c in child.children if c.type in ("identifier", "field_expression", "scoped_identifier")), None)
            if func_part:
                name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(caller_id=caller_id, callee_name=name, edge_type="calls", file=file_path))
        elif child.type == "method_call_expression":
            name_node = next((c for c in child.children if c.type == "field_identifier"), None)
            if name_node:
                edges.append(CallEdge(caller_id=caller_id, callee_name=_text(name_node, source), edge_type="calls", file=file_path))
        _collect_rust_calls(child, caller_id, file_path, source, edges)


# ── Go ────────────────────────────────────────────────────────────────────────

def _extract_go_doc(node: "Node", source: bytes) -> str:
    """Extract the leading // comment block immediately before a Go declaration."""
    before = source[:node.start_byte]
    lines = before.decode("utf-8", errors="replace").splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            doc_lines.insert(0, stripped[2:].strip())
        elif stripped == "":
            continue
        else:
            break
    return " ".join(doc_lines).strip()[:500]


_GO_FUNC_TYPES = {"function_declaration", "method_declaration"}


def _parse_go(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Extract functions and methods from a Go source file."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_go(root, file_path, module, source, nodes, edges)
    return nodes, edges


def _visit_go(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
) -> None:
    """Recursively visit a Go AST node, extracting function and type declarations."""
    if node.type in _GO_FUNC_TYPES:
        # For methods, receiver tells us the type
        receiver_type = ""
        if node.type == "method_declaration":
            recv = next((c for c in node.children if c.type == "parameter_list"), None)
            if recv:
                type_node = next((c for c in _walk(recv) if c.type == "type_identifier"), None)
                if type_node:
                    receiver_type = _text(type_node, source)

        name_node = next((c for c in node.children if c.type == "field_identifier"), None) or                     next((c for c in node.children if c.type == "identifier"), None)
        if not name_node:
            return

        func_name = _text(name_node, source)
        qual = f"{receiver_type}.{func_name}" if receiver_type else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if receiver_type else "function",
            signature=func_text.split("\n")[0].strip(), body=func_text[:2000],
            docstring=_extract_go_doc(node, source), body_hash=body_hash,
        ))
        _collect_go_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_go(child, file_path, module, source, nodes, edges)


def _collect_go_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Collect call_expression edges inside a Go function body."""
    for child in node.children:
        if child.type in _GO_FUNC_TYPES:
            continue
        if child.type == "call_expression":
            func_part = next((c for c in child.children if c.type in ("identifier", "selector_expression")), None)
            if func_part:
                name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(caller_id=caller_id, callee_name=name, edge_type="calls", file=file_path))
        _collect_go_calls(child, caller_id, file_path, source, edges)


# ── Java ──────────────────────────────────────────────────────────────────────

def _extract_java_javadoc(node: "Node", source: bytes) -> str:
    """Extract the /** ... */ Javadoc block immediately before a Java node."""
    before = source[:node.start_byte]
    end_idx = before.rfind(b"*/")
    if end_idx < 0:
        return ""
    start_idx = before.rfind(b"/**", 0, end_idx + 2)
    if start_idx < 0:
        return ""
    if before[end_idx + 2:].strip():
        return ""
    content = before[start_idx + 3:end_idx].decode("utf-8", errors="replace")
    lines = [line.strip().lstrip("* ") for line in content.splitlines()]
    return " ".join(l for l in lines if l).strip()[:500]


_JAVA_CLASS_TYPES = {"class_declaration", "interface_declaration", "enum_declaration", "annotation_type_declaration"}
_JAVA_FUNC_TYPES = {"method_declaration", "constructor_declaration"}


def _parse_java(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Extract classes and methods from a Java source file."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_java(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_java(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    """Recursively visit a Java AST node."""
    if node.type in _JAVA_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="",
                docstring=_extract_java_javadoc(node, source),
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
            ))
            for child in node.children:
                _visit_java(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type in _JAVA_FUNC_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if not name_node:
            return
        func_name = _text(name_node, source)
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=func_text.split("\n")[0].strip(), body=func_text[:2000],
            docstring=_extract_java_javadoc(node, source), body_hash=body_hash,
        ))
        _collect_java_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_java(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_java_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Collect method_invocation and object_creation_expression edges."""
    for child in node.children:
        if child.type in _JAVA_FUNC_TYPES:
            continue
        if child.type in ("method_invocation", "object_creation_expression"):
            name_node = next((c for c in child.children if c.type == "identifier"), None)
            if name_node:
                edges.append(CallEdge(caller_id=caller_id, callee_name=_text(name_node, source), edge_type="calls", file=file_path))
        _collect_java_calls(child, caller_id, file_path, source, edges)


# ── C++ ───────────────────────────────────────────────────────────────────────

def _extract_cpp_doc(node: "Node", source: bytes) -> str:
    """Extract leading // or /** doc comment before a C++ node."""
    before = source[:node.start_byte]
    # Try /** */ first
    end_idx = before.rfind(b"*/")
    if end_idx >= 0:
        start_idx = before.rfind(b"/**", 0, end_idx + 2)
        if start_idx >= 0 and not before[end_idx + 2:].strip():
            content = before[start_idx + 3:end_idx].decode("utf-8", errors="replace")
            lines = [line.strip().lstrip("* ") for line in content.splitlines()]
            return " ".join(l for l in lines if l).strip()[:500]
    # Fall back to leading // block
    lines = before.decode("utf-8", errors="replace").splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("//"):
            doc_lines.insert(0, stripped[2:].strip())
        elif stripped == "":
            continue
        else:
            break
    return " ".join(doc_lines).strip()[:500]


def _parse_cpp(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Extract functions and classes from a C++ source file."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_cpp(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_cpp(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    """Recursively visit a C++ AST node."""
    if node.type in ("class_specifier", "struct_specifier"):
        name_node = next((c for c in node.children if c.type == "type_identifier"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="",
                docstring=_extract_cpp_doc(node, source),
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
            ))
            for child in node.children:
                _visit_cpp(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type == "function_definition":
        # C++ function names live inside the declarator chain
        func_name = _cpp_func_name(node, source)
        if not func_name:
            return
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=func_text.split("\n")[0].strip(), body=func_text[:2000],
            docstring=_extract_cpp_doc(node, source), body_hash=body_hash,
        ))
        _collect_cpp_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_cpp(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _cpp_func_name(node: "Node", source: bytes) -> str:
    """Extract the function name from a C++ function_definition node."""
    for child in node.children:
        if child.type in ("function_declarator", "reference_declarator", "pointer_declarator"):
            return _cpp_func_name(child, source)
        if child.type == "qualified_identifier":
            # e.g. MyClass::method → take the last segment
            name_node = next((c for c in reversed(child.children) if c.type == "identifier"), None)
            return _text(name_node, source) if name_node else ""
        if child.type == "identifier":
            return _text(child, source)
    return ""


def _collect_cpp_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Collect call_expression edges inside a C++ function body."""
    for child in node.children:
        if child.type == "function_definition":
            continue
        if child.type == "call_expression":
            func_part = next((c for c in child.children if c.type in ("identifier", "field_expression", "qualified_identifier")), None)
            if func_part:
                name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(caller_id=caller_id, callee_name=name, edge_type="calls", file=file_path))
        _collect_cpp_calls(child, caller_id, file_path, source, edges)


# ── C# ────────────────────────────────────────────────────────────────────────

def _extract_csharp_doc(node: "Node", source: bytes) -> str:
    """Extract leading /// XML-doc comment lines before a C# node."""
    before = source[:node.start_byte]
    lines = before.decode("utf-8", errors="replace").splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("///"):
            doc_lines.insert(0, stripped[3:].strip())
        elif stripped == "" or stripped.startswith("["):
            continue
        else:
            break
    return " ".join(doc_lines).strip()[:500]


_CSHARP_CLASS_TYPES = {"class_declaration", "interface_declaration", "struct_declaration", "record_declaration"}
_CSHARP_FUNC_TYPES = {"method_declaration", "constructor_declaration", "local_function_statement"}


def _parse_csharp(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Extract classes and methods from a C# source file."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_csharp(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_csharp(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    """Recursively visit a C# AST node."""
    if node.type in _CSHARP_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="",
                docstring=_extract_csharp_doc(node, source),
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
            ))
            for child in node.children:
                _visit_csharp(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type in _CSHARP_FUNC_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if not name_node:
            return
        func_name = _text(name_node, source)
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=func_text.split("\n")[0].strip(), body=func_text[:2000],
            docstring=_extract_csharp_doc(node, source), body_hash=body_hash,
        ))
        _collect_csharp_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_csharp(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_csharp_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Collect invocation_expression edges inside a C# method body."""
    for child in node.children:
        if child.type in _CSHARP_FUNC_TYPES:
            continue
        if child.type == "invocation_expression":
            func_part = next((c for c in child.children if c.type in ("identifier", "member_access_expression")), None)
            if func_part:
                name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(caller_id=caller_id, callee_name=name, edge_type="calls", file=file_path))
        _collect_csharp_calls(child, caller_id, file_path, source, edges)


# ── Ruby ──────────────────────────────────────────────────────────────────────

def _extract_ruby_doc(node: "Node", source: bytes) -> str:
    """Extract leading # comment lines before a Ruby method or class."""
    before = source[:node.start_byte]
    lines = before.decode("utf-8", errors="replace").splitlines()
    doc_lines = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("#"):
            doc_lines.insert(0, stripped[1:].strip())
        elif stripped == "":
            continue
        else:
            break
    return " ".join(doc_lines).strip()[:500]


_RUBY_CLASS_TYPES = {"class", "module"}
_RUBY_FUNC_TYPES = {"method", "singleton_method"}


def _parse_ruby(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """Extract classes, modules, and methods from a Ruby source file."""
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_ruby(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_ruby(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    """Recursively visit a Ruby AST node."""
    if node.type in _RUBY_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type in ("constant", "scope_resolution")), None)
        if name_node:
            class_name = _text(name_node, source).split("::")[-1]
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="",
                docstring=_extract_ruby_doc(node, source),
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
            ))
            for child in node.children:
                _visit_ruby(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type in _RUBY_FUNC_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if not name_node:
            return
        func_name = _text(name_node, source)
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=func_text.split("\n")[0].strip(), body=func_text[:2000],
            docstring=_extract_ruby_doc(node, source), body_hash=body_hash,
        ))
        _collect_ruby_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_ruby(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_ruby_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    """Collect call and method_call edges inside a Ruby method body."""
    for child in node.children:
        if child.type in _RUBY_FUNC_TYPES:
            continue
        if child.type in ("call", "method_call"):
            name_node = next((c for c in child.children if c.type in ("identifier", "constant")), None)
            if name_node:
                edges.append(CallEdge(caller_id=caller_id, callee_name=_text(name_node, source), edge_type="calls", file=file_path))
        _collect_ruby_calls(child, caller_id, file_path, source, edges)

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
    for ext in (".py", ".ts", ".tsx", ".js", ".jsx",
               ".rs", ".go", ".java", ".cpp", ".cc", ".hpp", ".cs", ".rb"):
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    return rel.replace("/", ".").strip(".")
