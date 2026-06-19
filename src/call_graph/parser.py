from __future__ import annotations

import hashlib
import os
import re as _re
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

try:
    import tree_sitter_swift as tsswift
    _HAS_SWIFT = True
except ImportError:
    _HAS_SWIFT = False

try:
    import tree_sitter_kotlin as tskotlin
    _HAS_KOTLIN = True
except ImportError:
    _HAS_KOTLIN = False

try:
    import tree_sitter_php as tsphp
    _HAS_PHP = True
except ImportError:
    _HAS_PHP = False

# ── Generic fallback grammars ─────────────────────────────────────────────────

try:
    import tree_sitter_bash as tsbash
    _HAS_BASH = True
except ImportError:
    _HAS_BASH = False

try:
    import tree_sitter_lua as tslua
    _HAS_LUA = True
except ImportError:
    _HAS_LUA = False

try:
    import tree_sitter_scala as tsscala
    _HAS_SCALA = True
except ImportError:
    _HAS_SCALA = False

try:
    import tree_sitter_c as tsc
    _HAS_C = True
except ImportError:
    _HAS_C = False

try:
    import tree_sitter_ocaml as tsocaml
    _HAS_OCAML = True
except ImportError:
    _HAS_OCAML = False

try:
    import tree_sitter_elixir as tselixir
    _HAS_ELIXIR = True
except ImportError:
    _HAS_ELIXIR = False

try:
    import tree_sitter_haskell as tshaskell
    _HAS_HASKELL = True
except ImportError:
    _HAS_HASKELL = False

try:
    import tree_sitter_zig as tszig
    _HAS_ZIG = True
except ImportError:
    _HAS_ZIG = False

try:
    import tree_sitter_groovy as tsgroovy
    _HAS_GROOVY = True
except ImportError:
    _HAS_GROOVY = False

try:
    import tree_sitter_perl as tsperl
    _HAS_PERL = True
except ImportError:
    _HAS_PERL = False

try:
    import tree_sitter_commonlisp as tscommonlisp
    _HAS_COMMONLISP = True
except ImportError:
    _HAS_COMMONLISP = False

try:
    import tree_sitter_fortran as tsfortran
    _HAS_FORTRAN = True
except ImportError:
    _HAS_FORTRAN = False

try:
    import tree_sitter_solidity as tssolidity
    _HAS_SOLIDITY = True
except ImportError:
    _HAS_SOLIDITY = False

try:
    import tree_sitter_julia as tsjulia
    _HAS_JULIA = True
except ImportError:
    _HAS_JULIA = False

try:
    import tree_sitter_odin as tsodin
    _HAS_ODIN = True
except ImportError:
    _HAS_ODIN = False

try:
    import tree_sitter_matlab as tsmatlab
    _HAS_MATLAB = True
except ImportError:
    _HAS_MATLAB = False


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
    start_line: int = 0       # 1-indexed source line where the function/class starts
    end_line: int = 0         # 1-indexed source line where the function/class ends (inclusive)
    return_type: str = ""     # parsed return type annotation ("str", "list[dict]", etc.)
    is_async: bool = False    # true for async def / async fn / async function
    parameter_names: list = field(default_factory=list)  # ["self", "project_id", ...]
    enclosing_class: str = "" # bare class name when this is a method; "" for top-level
    structural_layer: str = "precision"  # "precision" | "generic" — quality signal for callers


@dataclass
class CallEdge:
    caller_id: str
    callee_name: str  # unresolved bare name; resolver in storage layer
    edge_type: str    # "calls" | "imports" | "inherits"
    file: str


def _find_param_close(text: str) -> tuple[int, int]:
    """Return (open_idx, close_idx) of the parameter list in a function definition.

    Uses bracket-depth counting so it works on both single-line signatures and
    multi-line function bodies. Returns (-1, -1) if no matching pair is found.
    """
    start = text.find("(")
    if start == -1:
        return -1, -1
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return start, i
    return start, -1


def _extract_return_type(text: str) -> str:
    """Extract return type from a function signature or body text.

    Handles Python/Rust/Go (-> Type) and TypeScript/C# ((): Type).
    Looks only after the closing ) of the parameter list to avoid false
    matches inside the function body.
    Returns "" for Java/C++/Ruby where the return type precedes the name.
    """
    _, close = _find_param_close(text)
    after = text[close + 1 :] if close != -1 else text
    # Python/Rust/Go: ) -> Type:
    m = _re.search(r"->\s*([^:{;\n]+)", after)
    if m:
        return m.group(1).strip()
    # TypeScript/C#: ): Type {
    m = _re.search(r"\)\s*:\s*([^{;\n]+)", text if close == -1 else text[close:])
    if m:
        return m.group(1).strip()
    return ""


def _extract_param_names(text: str) -> list:
    """Extract parameter names from a function definition string or body.

    Works on both single-line signatures and multi-line function definitions
    by using bracket-depth counting to locate the parameter list.
    Handles Python, TypeScript, Rust, Go, Java, C++, C#, Ruby.
    Skips self/cls for cleaner output.
    """
    start, end = _find_param_close(text)
    if start == -1 or end == -1 or start >= end:
        return []
    params_str = text[start + 1 : end].strip()
    if not params_str:
        return []
    # Split on commas respecting bracket depth
    depth, parts, current = 0, [], ""
    for ch in params_str:
        if ch in "([{<":
            depth += 1; current += ch
        elif ch in ")]}>" :
            depth -= 1; current += ch
        elif ch == "," and depth == 0:
            parts.append(current.strip()); current = ""
        else:
            current += ch
    if current.strip():
        parts.append(current.strip())
    names = []
    for p in parts:
        p = p.strip()
        if not p or p in ("*", "/", "..."):
            continue
        p_clean = p.lstrip("*&")
        name = _re.split(r"[:\s=]", p_clean)[0].strip().lstrip("*")
        if name and name not in ("self", "cls") and (name.isidentifier() or name.startswith("_")):
            names.append(name)
    return names


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
        if _HAS_TREE_SITTER and _HAS_SWIFT:
            self._parsers[".swift"] = Parser(Language(tsswift.language()))
        if _HAS_TREE_SITTER and _HAS_KOTLIN:
            self._parsers[".kt"] = Parser(Language(tskotlin.language()))
            self._parsers[".kts"] = Parser(Language(tskotlin.language()))
        if _HAS_TREE_SITTER and _HAS_PHP:
            self._parsers[".php"] = Parser(Language(tsphp.language_php()))
            self._parsers[".phtml"] = Parser(Language(tsphp.language_php()))

        # Generic fallback parsers — lower fidelity (no class membership, async,
        # or return types for most), but enables semantic search and blast-radius
        # coverage for languages without a precision parser.
        self._generic_parsers: dict[str, tuple["Parser", str]] = {}
        if _HAS_TREE_SITTER and _HAS_BASH:
            for ext in (".sh", ".bash", ".zsh", ".fish"):
                self._generic_parsers[ext] = (Parser(Language(tsbash.language())), "bash")
        if _HAS_TREE_SITTER and _HAS_LUA:
            self._generic_parsers[".lua"] = (Parser(Language(tslua.language())), "lua")
        if _HAS_TREE_SITTER and _HAS_SCALA:
            for ext in (".scala", ".sc"):
                self._generic_parsers[ext] = (Parser(Language(tsscala.language())), "scala")
        if _HAS_TREE_SITTER and _HAS_C:
            for ext in (".c", ".h"):
                self._generic_parsers[ext] = (Parser(Language(tsc.language())), "c")
        if _HAS_TREE_SITTER and _HAS_OCAML:
            for ext in (".ml", ".mli"):
                self._generic_parsers[ext] = (Parser(Language(tsocaml.language_ocaml())), "ocaml")
        if _HAS_TREE_SITTER and _HAS_ELIXIR:
            for ext in (".ex", ".exs"):
                self._generic_parsers[ext] = (Parser(Language(tselixir.language())), "elixir")
        if _HAS_TREE_SITTER and _HAS_HASKELL:
            for ext in (".hs", ".lhs"):
                self._generic_parsers[ext] = (Parser(Language(tshaskell.language())), "haskell")
        if _HAS_TREE_SITTER and _HAS_ZIG:
            self._generic_parsers[".zig"] = (Parser(Language(tszig.language())), "zig")
        if _HAS_TREE_SITTER and _HAS_GROOVY:
            for ext in (".groovy", ".gvy", ".gy", ".gsh"):
                self._generic_parsers[ext] = (Parser(Language(tsgroovy.language())), "groovy")
        if _HAS_TREE_SITTER and _HAS_PERL:
            for ext in (".pl", ".pm", ".t"):
                self._generic_parsers[ext] = (Parser(Language(tsperl.language())), "perl")
        if _HAS_TREE_SITTER and _HAS_COMMONLISP:
            for ext in (".lisp", ".cl", ".lsp"):
                self._generic_parsers[ext] = (Parser(Language(tscommonlisp.language())), "commonlisp")
        if _HAS_TREE_SITTER and _HAS_FORTRAN:
            for ext in (".f90", ".f95", ".f", ".f03", ".f08", ".for"):
                self._generic_parsers[ext] = (Parser(Language(tsfortran.language())), "fortran")
        if _HAS_TREE_SITTER and _HAS_SOLIDITY:
            self._generic_parsers[".sol"] = (Parser(Language(tssolidity.language())), "solidity")
        if _HAS_TREE_SITTER and _HAS_JULIA:
            self._generic_parsers[".jl"] = (Parser(Language(tsjulia.language())), "julia")
        if _HAS_TREE_SITTER and _HAS_ODIN:
            self._generic_parsers[".odin"] = (Parser(Language(tsodin.language())), "odin")
        if _HAS_TREE_SITTER and _HAS_MATLAB:
            for ext in (".m", ".mlx"):
                self._generic_parsers[ext] = (Parser(Language(tsmatlab.language())), "matlab")

    @property
    def supported_extensions(self) -> set[str]:
        """Return the set of file extensions this parser can handle."""
        return set(self._parsers.keys()) | set(self._generic_parsers.keys())

    def parse_file(
        self, file_path: str, content: str, project_root: str = ""
    ) -> tuple[list[FunctionNode], list[CallEdge]]:
        """Parse a source file and return (nodes, edges) for all functions and calls."""
        ext = Path(file_path).suffix.lower()
        source = content.encode("utf-8", errors="replace")
        module = _path_to_module(file_path, project_root)

        if ext not in self._parsers:
            # Try generic fallback before giving up
            if ext in self._generic_parsers:
                gparser, lang = self._generic_parsers[ext]
                tree = gparser.parse(source)
                return _parse_generic(tree.root_node, file_path, module, source, lang)
            return [], []

        tree = self._parsers[ext].parse(source)

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
        if ext == ".swift":
            return _parse_swift(tree.root_node, file_path, module, source)
        if ext in (".kt", ".kts"):
            return _parse_kotlin(tree.root_node, file_path, module, source)
        if ext in (".php", ".phtml"):
            return _parse_php(tree.root_node, file_path, module, source)
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
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="", docstring="",
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            ))
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
            is_async=signature.startswith("async "),
            return_type=_extract_return_type(func_text),
            parameter_names=_extract_param_names(func_text),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
    """Extract consecutive # comment lines before the function block.

    Tree-sitter places standalone comment lines between the colon and the block
    as direct children of function_definition, not inside the block itself:

        function_definition
            def | identifier | parameters | :
            comment   ← here, not inside block
            comment
            block
                ...

    We collect all comment children that appear before the block node.
    """
    lines = []
    for child in func_node.children:
        if child.type == "block":
            break
        if child.type == "comment":
            lines.append(_text(child, source).lstrip("# ").strip())
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
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="", docstring="",
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            ))
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
            is_async=signature.startswith("async "),
            return_type=_extract_return_type(func_text),
            parameter_names=_extract_param_names(func_text),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
        _sig = func_text.split("\n")[0].strip()
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=_sig, body=func_text[:2000],
            docstring=_extract_rust_doc(node, source), body_hash=body_hash,
            is_async=_sig.startswith("async "),
            return_type=_extract_return_type(_sig),
            parameter_names=_extract_param_names(_sig),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
        _sig = func_text.split("\n")[0].strip()
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if receiver_type else "function",
            signature=_sig, body=func_text[:2000],
            docstring=_extract_go_doc(node, source), body_hash=body_hash,
            is_async=False,
            return_type=_extract_return_type(_sig),
            parameter_names=_extract_param_names(_sig),
            enclosing_class=receiver_type or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
        _sig = func_text.split("\n")[0].strip()
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=_sig, body=func_text[:2000],
            docstring=_extract_java_javadoc(node, source), body_hash=body_hash,
            is_async=False, return_type="", parameter_names=_extract_param_names(_sig),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
        _sig = func_text.split("\n")[0].strip()
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=_sig, body=func_text[:2000],
            docstring=_extract_cpp_doc(node, source), body_hash=body_hash,
            is_async=False, return_type="", parameter_names=_extract_param_names(_sig),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
        _sig = func_text.split("\n")[0].strip()
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=_sig, body=func_text[:2000],
            docstring=_extract_csharp_doc(node, source), body_hash=body_hash,
            is_async=_sig.startswith("async "),
            return_type=_extract_return_type(_sig),
            parameter_names=_extract_param_names(_sig),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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
        _sig = func_text.split("\n")[0].strip()
        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=_sig, body=func_text[:2000],
            docstring=_extract_ruby_doc(node, source), body_hash=body_hash,
            is_async=False, return_type="", parameter_names=_extract_param_names(_sig),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
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

# ── Swift ────────────────────────────────────────────────────────────────────

_SWIFT_CLASS_TYPES = {
    "class_declaration", "struct_declaration", "protocol_declaration",
    "enum_declaration", "extension_declaration",
}
_SWIFT_FUNC_TYPES = {"function_declaration", "init_declaration", "protocol_function_declaration"}


def _parse_swift(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_swift(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_swift(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    if node.type in _SWIFT_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type == "type_identifier"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="", docstring="",
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            ))
            for child in node.children:
                _visit_swift(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type in _SWIFT_FUNC_TYPES:
        if node.type == "init_declaration":
            func_name = "init"
        else:
            name_node = next((c for c in node.children if c.type == "simple_identifier"), None)
            if not name_node:
                return
            func_name = _text(name_node, source)

        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        sig = func_text.split("\n")[0].strip()

        is_async = any(c.type == "async" for c in node.children)

        # Return type follows "->" child
        return_type = ""
        found_arrow = False
        for c in node.children:
            if c.type == "->":
                found_arrow = True
            elif found_arrow and c.type in (
                "user_type", "type_identifier", "optional_type",
                "array_type", "dictionary_type", "tuple_type",
            ):
                return_type = _text(c, source)
                break

        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=sig, body=func_text[:2000], docstring="",
            body_hash=body_hash, is_async=is_async,
            return_type=return_type,
            parameter_names=_extract_param_names(sig),
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
        ))
        _collect_swift_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_swift(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_swift_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    for child in node.children:
        if child.type in _SWIFT_FUNC_TYPES:
            continue
        if child.type == "call_expression":
            # First child is the called expression (simple_identifier or navigation_expression)
            func_part = child.children[0] if child.children else None
            if func_part:
                if func_part.type == "simple_identifier":
                    name = _text(func_part, source)
                elif func_part.type in ("navigation_expression", "explicit_member_expression"):
                    # Swift navigation_expression: "JSON.parse" or "network.get"
                    # The method name is after the last dot.
                    full = _text(func_part, source)
                    name = full.rsplit(".", 1)[-1] if "." in full else full
                else:
                    name = ""
                if name:
                    edges.append(CallEdge(
                        caller_id=caller_id, callee_name=name,
                        edge_type="calls", file=file_path,
                    ))
        _collect_swift_calls(child, caller_id, file_path, source, edges)


# ── Kotlin ───────────────────────────────────────────────────────────────────

_KOTLIN_CLASS_TYPES = {
    "class_declaration", "object_declaration", "interface_declaration",
}
_KOTLIN_FUNC_TYPES = {"function_declaration", "anonymous_initializer"}


def _parse_kotlin(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_kotlin(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_kotlin(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    if node.type in _KOTLIN_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="", docstring="",
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            ))
            for child in node.children:
                _visit_kotlin(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type == "function_declaration":
        name_node = next((c for c in node.children if c.type == "identifier"), None)
        if not name_node:
            return
        func_name = _text(name_node, source)
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        sig = func_text.split("\n")[0].strip()

        # suspend keyword in modifiers → treat as async
        modifiers = next((c for c in node.children if c.type == "modifiers"), None)
        is_async = bool(modifiers and any(
            any(gc.type == "suspend" for gc in c.children)
            for c in modifiers.children if c.type == "function_modifier"
        ))

        # Return type: user_type or nullable_type child of function_declaration
        # that immediately follows function_value_parameters. Regex on the
        # signature string fails for single-expression functions (fun f(): T = expr).
        return_type = ""
        past_params = False
        for c in node.children:
            if c.type == "function_value_parameters":
                past_params = True
            elif past_params and c.type in ("user_type", "nullable_type", "function_type"):
                return_type = _text(c, source)
                break

        # Parameters from function_value_parameters
        param_names = []
        params_node = next(
            (c for c in node.children if c.type == "function_value_parameters"), None
        )
        if params_node:
            for param in params_node.children:
                if param.type == "parameter":
                    pname = next((c for c in param.children if c.type == "identifier"), None)
                    if pname:
                        param_names.append(_text(pname, source))

        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=sig, body=func_text[:2000], docstring="",
            body_hash=body_hash, is_async=is_async,
            return_type=return_type, parameter_names=param_names,
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
        ))
        _collect_kotlin_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_kotlin(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_kotlin_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    for child in node.children:
        if child.type == "function_declaration":
            continue
        if child.type == "call_expression":
            # First child is simple_identifier or navigation_expression
            func_part = child.children[0] if child.children else None
            if func_part:
                if func_part.type == "identifier":
                    name = _text(func_part, source)
                elif func_part.type == "navigation_expression":
                    name = next(
                        (_text(c, source) for c in reversed(func_part.children)
                         if c.type == "identifier"),
                        "",
                    )
                else:
                    name = _resolve_call_name(func_part, source)
                if name:
                    edges.append(CallEdge(
                        caller_id=caller_id, callee_name=name,
                        edge_type="calls", file=file_path,
                    ))
        _collect_kotlin_calls(child, caller_id, file_path, source, edges)


# ── PHP ──────────────────────────────────────────────────────────────────────

_PHP_CLASS_TYPES = {
    "class_declaration", "trait_declaration", "interface_declaration",
}
_PHP_FUNC_TYPES = {"function_definition", "method_declaration"}
_PHP_CALL_TYPES = {
    "function_call_expression", "member_call_expression",
    "scoped_call_expression", "nullsafe_member_call_expression",
}


def _parse_php(
    root: "Node", file_path: str, module: str, source: bytes
) -> tuple[list[FunctionNode], list[CallEdge]]:
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    _visit_php(root, file_path, module, source, nodes, edges, parent_class=None)
    return nodes, edges


def _visit_php(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    parent_class: str | None,
) -> None:
    if node.type in _PHP_CLASS_TYPES:
        name_node = next((c for c in node.children if c.type == "name"), None)
        if name_node:
            class_name = _text(name_node, source)
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="", docstring="",
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            ))
            for child in node.children:
                _visit_php(child, file_path, module, source, nodes, edges, parent_class=class_name)
        return

    if node.type in _PHP_FUNC_TYPES:
        name_node = next((c for c in node.children if c.type == "name"), None)
        if not name_node:
            return
        func_name = _text(name_node, source)
        qual = f"{parent_class}.{func_name}" if parent_class else func_name
        func_id = f"{module}.{qual}"
        func_text = _node_text(node, source)
        body_hash = hashlib.sha256(func_text.encode("utf-8", errors="replace")).hexdigest()[:16]
        sig = func_text.split("\n")[0].strip()

        # PHP has no native async
        is_async = False

        # Return type: named_type or union_type or intersection_type after ":"
        return_type = ""
        found_colon = False
        for c in node.children:
            if c.type == ":":
                found_colon = True
            elif found_colon and c.type in (
                "named_type", "primitive_type", "union_type",
                "intersection_type", "nullable_type",
            ):
                return_type = _text(c, source)
                break

        # Parameters: variable_name > name children of formal_parameters
        param_names: list[str] = []
        params_node = next(
            (c for c in node.children if c.type == "formal_parameters"), None
        )
        if params_node:
            for param in params_node.children:
                if param.type in ("simple_parameter", "variadic_parameter", "property_promotion_parameter"):
                    vname = next((c for c in param.children if c.type == "variable_name"), None)
                    if vname:
                        # variable_name: "$ name" — take the "name" child, not "$"
                        n = next((c for c in vname.children if c.type == "name"), None)
                        if n:
                            param_names.append(_text(n, source))

        nodes.append(FunctionNode(
            id=func_id, name=qual, file=file_path, module=module,
            type="method" if parent_class else "function",
            signature=sig, body=func_text[:2000], docstring="",
            body_hash=body_hash, is_async=is_async,
            return_type=return_type, parameter_names=param_names,
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
        ))
        _collect_php_calls(node, func_id, file_path, source, edges)
        return

    for child in node.children:
        _visit_php(child, file_path, module, source, nodes, edges, parent_class=parent_class)


def _collect_php_calls(
    node: "Node", caller_id: str, file_path: str, source: bytes, edges: list[CallEdge]
) -> None:
    for child in node.children:
        if child.type in _PHP_FUNC_TYPES:
            continue
        if child.type in _PHP_CALL_TYPES:
            # Last "name" child is the callee — for scoped_call_expression (JSON::parse)
            # there are two name nodes (class, method); for others there is one.
            name_nodes = [c for c in child.children if c.type == "name"]
            name_node = name_nodes[-1] if name_nodes else None
            if name_node:
                edges.append(CallEdge(
                    caller_id=caller_id, callee_name=_text(name_node, source),
                    edge_type="calls", file=file_path,
                ))
        _collect_php_calls(child, caller_id, file_path, source, edges)


# ── Generic fallback parser ───────────────────────────────────────────────────

# Function and class node types per language.
_GENERIC_FUNC_TYPES: dict[str, frozenset[str]] = {
    "bash":       frozenset({"function_definition"}),
    "lua":        frozenset({"function_declaration"}),
    "scala":      frozenset({"function_definition"}),
    "c":          frozenset({"function_definition"}),
    "ocaml":      frozenset({"value_definition"}),
    "haskell":    frozenset({"function"}),
    "zig":        frozenset({"function_declaration"}),
    "groovy":     frozenset({"method_declaration"}),
    "perl":       frozenset({"subroutine_declaration_statement"}),
    "commonlisp": frozenset({"defun"}),
    "fortran":    frozenset({"function", "subroutine"}),
    "solidity":   frozenset({"function_definition"}),
    "julia":      frozenset({"function_definition"}),
    "odin":       frozenset({"procedure_declaration"}),
    "matlab":     frozenset({"function_definition"}),
    # elixir: handled via identifier-verb inspection — not a fixed type name
}
_GENERIC_CLASS_TYPES: dict[str, frozenset[str]] = {
    "scala":    frozenset({"class_definition", "object_definition", "trait_definition"}),
    "c":        frozenset({"struct_specifier"}),
    "ocaml":    frozenset({"module_definition"}),
    "groovy":   frozenset({"class_declaration"}),
    "solidity": frozenset({"contract_declaration"}),
}

# Node types to skip entirely (don't recurse into them) per language.
# Prevents false-positive function matches inside type annotations or declarations.
_GENERIC_SKIP_TYPES: dict[str, frozenset[str]] = {
    # Haskell `signature` nodes contain `function` type nodes (e.g. "Int -> Int")
    # that share the same node type as function definitions — skip them entirely.
    "haskell": frozenset({"signature"}),
}

# Identifier-like child types tried in order when extracting a function name.
_GENERIC_ID_TYPES = (
    "identifier", "name", "value_name", "word", "variable", "simple_identifier",
    "bareword",   # Perl subroutine names
    "sym_lit",    # Common Lisp symbol literals
)

# Call-expression heuristic: any node whose type contains these substrings.
_GENERIC_CALL_SUBSTRINGS = ("call_expression", "function_call", "application", "invocation")


def _generic_func_name(node: "Node", source: bytes, lang: str) -> str:
    """Extract the function/class name from a generic language AST node."""
    if lang == "c":
        # function_definition → function_declarator → identifier
        decl = next((c for c in node.children if c.type == "function_declarator"), None)
        if decl:
            id_n = next((c for c in decl.children if c.type == "identifier"), None)
            return _text(id_n, source) if id_n else ""
        return ""

    if lang == "ocaml":
        # value_definition → let_binding → value_name
        binding = next((c for c in node.children if c.type == "let_binding"), None)
        if binding:
            vn = next((c for c in binding.children if c.type == "value_name"), None)
            return _text(vn, source) if vn else ""
        return ""

    if lang == "commonlisp":
        # defun → defun_header → sym_lit (the function name)
        header = next((c for c in node.children if c.type == "defun_header"), None)
        if header:
            sym = next((c for c in header.children if c.type == "sym_lit"), None)
            return _text(sym, source) if sym else ""
        return ""

    if lang == "fortran":
        # function/subroutine container → function_statement/subroutine_statement → name
        stmt = next(
            (c for c in node.children
             if c.type in ("function_statement", "subroutine_statement")),
            None,
        )
        if stmt:
            n = next((c for c in stmt.children if c.type == "name"), None)
            return _text(n, source) if n else ""
        return ""

    if lang == "julia":
        # function_definition → signature → call_expression → identifier
        sig = next((c for c in node.children if c.type == "signature"), None)
        if sig:
            call = next((c for c in sig.children if c.type == "call_expression"), None)
            if call:
                id_n = next((c for c in call.children if c.type == "identifier"), None)
                return _text(id_n, source) if id_n else ""
        return ""

    if lang == "elixir":
        # call node: identifier tells us the verb (def/defp/defmodule)
        # arguments holds name as an identifier or nested call (when there are params)
        id_n = next((c for c in node.children if c.type == "identifier"), None)
        if not id_n:
            return ""
        verb = _text(id_n, source)
        args = next((c for c in node.children if c.type == "arguments"), None)
        if not args:
            return ""
        if verb == "defmodule":
            alias_n = next((c for c in args.children if c.type == "alias"), None)
            return _text(alias_n, source) if alias_n else ""
        # def/defp: first child of arguments is identifier (no params) or call (with params)
        first = next((c for c in args.children if c.type in ("identifier", "call")), None)
        if first is None:
            return ""
        if first.type == "call":
            fn = next((c for c in first.children if c.type == "identifier"), None)
            return _text(fn, source) if fn else ""
        return _text(first, source)

    # Default: first child matching a known identifier type
    for child in node.children:
        if child.type in _GENERIC_ID_TYPES:
            name = _text(child, source).rstrip("(").strip()
            if name and (name.isidentifier() or (name and name[0].isalpha())):
                return name
    return ""


def _is_elixir_func(node: "Node", source: bytes) -> bool:
    if node.type != "call":
        return False
    id_n = next((c for c in node.children if c.type == "identifier"), None)
    return bool(id_n and _text(id_n, source) in ("def", "defp", "defmacro", "defmacrop"))


def _is_elixir_class(node: "Node", source: bytes) -> bool:
    if node.type != "call":
        return False
    id_n = next((c for c in node.children if c.type == "identifier"), None)
    return bool(id_n and _text(id_n, source) == "defmodule")


def _generic_collect_calls(
    node: "Node",
    caller_id: str,
    file_path: str,
    source: bytes,
    edges: list[CallEdge],
    func_types: frozenset[str],
) -> None:
    """Best-effort callee extraction using heuristics across grammars."""
    for child in node.children:
        if child.type in func_types:
            continue  # don't descend into nested definitions
        t = child.type.lower()
        if any(sub in t for sub in _GENERIC_CALL_SUBSTRINGS):
            name = next(
                (_text(gc, source) for gc in child.children if gc.type in _GENERIC_ID_TYPES),
                "",
            )
            if name and name.isidentifier():
                edges.append(CallEdge(
                    caller_id=caller_id, callee_name=name,
                    edge_type="calls", file=file_path,
                ))
        _generic_collect_calls(child, caller_id, file_path, source, edges, func_types)


def _parse_generic(
    root: "Node", file_path: str, module: str, source: bytes, lang: str = ""
) -> tuple[list[FunctionNode], list[CallEdge]]:
    """
    Generic parser for languages without a precision parser.

    Uses language profiles (_GENERIC_FUNC_TYPES / _GENERIC_CLASS_TYPES) to
    identify function and class nodes, then falls back to type-name heuristics
    for unknown grammars. Sets structural_layer='generic' on all emitted nodes.

    Fidelity relative to precision parsers:
    - Names: extracted where possible (language-specific for C, OCaml, Elixir)
    - Bodies: full text captured for embeddings / summaries
    - Call edges: best-effort via heuristic (may miss complex call sites)
    - is_async / return_type / parameter_names: always empty (not extracted)
    - Class membership: captured when class_types profile exists
    """
    nodes: list[FunctionNode] = []
    edges: list[CallEdge] = []
    func_types = _GENERIC_FUNC_TYPES.get(lang, frozenset())
    _visit_generic(root, file_path, module, source, nodes, edges, lang, func_types, None)
    return nodes, edges


def _visit_generic(
    node: "Node",
    file_path: str,
    module: str,
    source: bytes,
    nodes: list[FunctionNode],
    edges: list[CallEdge],
    lang: str,
    func_types: frozenset[str],
    parent_class: str | None,
) -> None:
    if node.type in _GENERIC_SKIP_TYPES.get(lang, frozenset()):
        return

    # ── Class / module detection ──────────────────────────────────────────────
    class_types = _GENERIC_CLASS_TYPES.get(lang, frozenset())
    is_class = (node.type in class_types) or (lang == "elixir" and _is_elixir_class(node, source))
    if is_class:
        class_name = _generic_func_name(node, source, lang)
        if class_name:
            class_id = f"{module}.{class_name}"
            sig = _node_text(node, source).split("\n")[0].strip()
            nodes.append(FunctionNode(
                id=class_id, name=class_name, file=file_path, module=module,
                type="class", signature=sig, body="", docstring="",
                body_hash=hashlib.sha256(sig.encode()).hexdigest()[:16],
                is_async=False, return_type="", parameter_names=[],
                enclosing_class=parent_class or "",
                start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
                structural_layer="generic",
            ))
            for child in node.children:
                _visit_generic(child, file_path, module, source, nodes, edges,
                               lang, func_types, parent_class=class_name)
        return

    # ── Function detection ────────────────────────────────────────────────────
    is_func = (node.type in func_types) or (lang == "elixir" and _is_elixir_func(node, source))
    if not is_func and not func_types:
        # Pure heuristic for completely unknown grammars
        t = node.type.lower()
        is_func = (
            ("function" in t or "method" in t)
            and "call" not in t and "type" not in t
            and ("definition" in t or "declaration" in t or "item" in t)
        )

    if is_func:
        func_name = _generic_func_name(node, source, lang)
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
            docstring="", body_hash=body_hash,
            is_async=False, return_type="", parameter_names=[],
            enclosing_class=parent_class or "",
            start_line=node.start_point[0] + 1, end_line=node.end_point[0] + 1,
            structural_layer="generic",
        ))
        _generic_collect_calls(node, func_id, file_path, source, edges, func_types)
        return

    for child in node.children:
        _visit_generic(child, file_path, module, source, nodes, edges,
                       lang, func_types, parent_class=parent_class)


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
               ".rs", ".go", ".java", ".cpp", ".cc", ".hpp", ".cs", ".rb",
               ".swift", ".kt", ".kts", ".php", ".phtml",
               ".sh", ".bash", ".zsh", ".fish",
               ".lua", ".scala", ".sc", ".c", ".h",
               ".ml", ".mli", ".ex", ".exs", ".hs", ".lhs",
               ".zig", ".groovy", ".gvy", ".gy", ".gsh",
               ".pl", ".pm", ".t", ".lisp", ".cl", ".lsp",
               ".f90", ".f95", ".f", ".f03", ".f08", ".for",
               ".sol", ".jl", ".odin", ".m", ".mlx"):
        if rel.endswith(ext):
            rel = rel[: -len(ext)]
            break
    return rel.replace("/", ".").strip(".")
