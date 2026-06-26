"""
Tests for ScipImporter — the SCIP JSON ingestion layer.

All tests use synthetic SCIP JSON built from helpers. No filesystem access
except where project_root path resolution is the subject. No database, no async.

Each test maps to a production scenario: a wrong node_id means callers can't
find the function; wrong is_external means the dependency graph is corrupt;
wrong signature means check_dependency returns unreadable output.
"""
import json
import pytest
from src.scip_import import (
    ScipImporter,
    _normalise_external_signature,
    _scip_sym_to_node_id,
)


# ── SCIP JSON fixture helpers ─────────────────────────────────────────────────

# Canonical SCIP symbol strings used across tests.
# Format: "scip-{lang} {lang} {package} {version} {path/descriptor}."
PY_INTERNAL   = "scip-python python myproject 1.0 src/mod.py:my_fn()."
PY_NUMPY      = "scip-python python numpy 1.23.5 numpy/`array`()."
PY_REQUESTS   = "scip-python python requests 2.28.0 requests/`get`()."
PY_SESSION_GET = "scip-python python requests 2.28.0 requests/`Session`#`get`()."
TS_USE_STATE  = "scip-typescript npm react 18.0.0 src/index.ts/useState()."


def _sym(
    symbol: str,
    docs: list[str] | None = None,
    refs: list[str] | None = None,
    ref_flags: dict[str, bool] | None = None,
) -> dict:
    """Build a SCIP symbol entry."""
    relationships = []
    for ref in (refs or []):
        flags = (ref_flags or {}).get(ref, {})
        rel = {"symbol": ref}
        rel.update(flags if flags else {"isReference": True})
        relationships.append(rel)
    return {
        "symbol": symbol,
        "documentation": docs or [],
        "relationships": relationships,
    }


def _doc(path: str = "src/mod.py", symbols: list | None = None) -> dict:
    return {"relativePath": path, "symbols": symbols or []}


def _scip(documents: list | None = None) -> str:
    return json.dumps({"documents": documents or []})


def _parse(documents: list | None = None, root: str = "") -> tuple:
    return ScipImporter(project_root=root).parse(_scip(documents))


# ── Tracer bullet ─────────────────────────────────────────────────────────────

class TestTracerBullet:
    def test_parse_returns_nodes_and_edges(self):
        """Minimal end-to-end: one internal symbol referencing one external."""
        nodes, edges = _parse([
            _doc("src/mod.py", [
                _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
            ])
        ])
        assert len(nodes) >= 1
        assert len(edges) >= 1


# ── Internal node extraction ──────────────────────────────────────────────────

class TestParseInternalNodes:
    def test_node_created_for_each_symbol(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
        ])])
        internal = [n for n in nodes if not n.is_external]
        assert len(internal) == 1

    def test_internal_node_is_external_false(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.is_external is False

    def test_node_name_is_bare_identifier(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.name == "my_fn"

    def test_node_signature_from_first_doc_line(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn(x: int) -> str:", "Does something."]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.signature == "def my_fn(x: int) -> str:"

    def test_node_docstring_from_remaining_doc_lines(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():", "First line.", "Second line."]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert "First line." in node.docstring
        assert "Second line." in node.docstring

    def test_node_docstring_empty_when_only_one_doc_line(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.docstring == ""

    def test_node_kind_is_function_by_default(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.type == "function"

    def test_node_kind_is_class_when_signature_has_class_keyword(self):
        class_sym = "scip-python python myproject 1.0 src/mod.py:MyClass#."
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(class_sym, docs=["class MyClass:"]),
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.type == "class"

    def test_body_hash_is_deterministic(self):
        """Same documentation always produces the same hash."""
        docs = ["def my_fn():", "Does something."]
        nodes_a, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL, docs=docs)])])
        nodes_b, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL, docs=docs)])])
        node_a = next(n for n in nodes_a if not n.is_external)
        node_b = next(n for n in nodes_b if not n.is_external)
        assert node_a.body_hash == node_b.body_hash

    def test_body_hash_changes_when_docs_change(self):
        nodes_a, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL, docs=["def my_fn():"])])])
        nodes_b, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL, docs=["def my_fn(x: int):"])])])
        ha = next(n for n in nodes_a if not n.is_external).body_hash
        hb = next(n for n in nodes_b if not n.is_external).body_hash
        assert ha != hb

    def test_duplicate_symbol_in_same_document_deduplicated(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
            _sym(PY_INTERNAL, docs=["def my_fn():"]),
        ])])
        internal = [n for n in nodes if not n.is_external]
        assert len(internal) == 1

    def test_node_without_documentation_still_created(self):
        """A symbol with no docs array still produces a node — id falls back to symbol string."""
        nodes, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL)])])
        internal = [n for n in nodes if not n.is_external]
        assert len(internal) == 1

    def test_multiple_documents_each_produce_nodes(self):
        sym_a = "scip-python python myproject 1.0 src/a.py:fn_a()."
        sym_b = "scip-python python myproject 1.0 src/b.py:fn_b()."
        nodes, _ = _parse([
            _doc("src/a.py", [_sym(sym_a, docs=["def fn_a():"])]),
            _doc("src/b.py", [_sym(sym_b, docs=["def fn_b():"])]),
        ])
        names = {n.name for n in nodes if not n.is_external}
        assert "fn_a" in names
        assert "fn_b" in names


# ── External stub creation ────────────────────────────────────────────────────

class TestParseExternalStubs:
    def test_external_reference_creates_stub_node(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        stubs = [n for n in nodes if n.is_external]
        assert len(stubs) == 1

    def test_stub_is_external_true(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        stub = next(n for n in nodes if n.is_external)
        assert stub.is_external is True

    def test_stub_id_has_external_library_prefix(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        stub = next(n for n in nodes if n.is_external)
        assert stub.id.startswith("external.numpy.")

    def test_stub_name_is_bare_identifier(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        stub = next(n for n in nodes if n.is_external)
        assert stub.name == "array"

    def test_stub_signature_is_normalised_not_raw_scip(self):
        """
        External stubs must carry a human-readable signature.
        Raw SCIP symbols like 'scip-python python numpy 1.23 numpy/`array`().'
        must not appear in results — callers (check_dependency, get_callees)
        would see unreadable output.
        """
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        stub = next(n for n in nodes if n.is_external)
        assert stub.signature == "numpy.array(...)"
        assert "scip-python" not in stub.signature
        assert "`" not in stub.signature

    def test_class_method_stub_signature_includes_class(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_SESSION_GET])
        ])])
        stub = next(n for n in nodes if n.is_external)
        assert stub.signature == "requests.Session.get(...)"

    def test_same_external_symbol_referenced_multiple_times_creates_one_stub(self):
        """
        100 internal functions calling numpy.array → 1 stub node, not 100.
        This is the deduplication invariant for external stubs.
        """
        sym_a = "scip-python python myproject 1.0 src/a.py:fn_a()."
        sym_b = "scip-python python myproject 1.0 src/b.py:fn_b()."
        nodes, _ = _parse([
            _doc("src/a.py", [_sym(sym_a, docs=["def fn_a():"], refs=[PY_NUMPY])]),
            _doc("src/b.py", [_sym(sym_b, docs=["def fn_b():"], refs=[PY_NUMPY])]),
        ])
        stubs = [n for n in nodes if n.is_external]
        assert len(stubs) == 1

    def test_different_external_symbols_each_get_a_stub(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY, PY_REQUESTS])
        ])])
        stubs = [n for n in nodes if n.is_external]
        assert len(stubs) == 2

    def test_internal_to_internal_reference_does_not_create_stub(self):
        """
        When function A calls function B (both in the project), only an edge
        is created — no external stub. External stubs are only for library symbols.
        """
        sym_a = "scip-python python myproject 1.0 src/mod.py:fn_a()."
        sym_b = "scip-python python myproject 1.0 src/mod.py:fn_b()."
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(sym_a, docs=["def fn_a():"], refs=[sym_b]),
            _sym(sym_b, docs=["def fn_b():"]),
        ])])
        stubs = [n for n in nodes if n.is_external]
        assert len(stubs) == 0


# ── Edge extraction ───────────────────────────────────────────────────────────

class TestParseEdges:
    def test_is_reference_true_creates_edge(self):
        _, edges = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        assert len(edges) == 1

    def test_edge_type_is_reference(self):
        _, edges = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        assert edges[0].edge_type == "reference"

    def test_edge_caller_id_is_internal_node_id(self):
        nodes, edges = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        internal = next(n for n in nodes if not n.is_external)
        assert edges[0].caller_id == internal.id

    def test_non_reference_relationship_does_not_create_edge(self):
        """isImplementation / isTypeDefinition etc. are not call edges."""
        _, edges = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"],
                 refs=[PY_NUMPY],
                 ref_flags={PY_NUMPY: {"isImplementation": True}})
        ])])
        assert len(edges) == 0

    def test_two_external_refs_create_two_edges(self):
        _, edges = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY, PY_REQUESTS])
        ])])
        assert len(edges) == 2

    def test_internal_to_internal_creates_edge(self):
        sym_a = "scip-python python myproject 1.0 src/mod.py:fn_a()."
        sym_b = "scip-python python myproject 1.0 src/mod.py:fn_b()."
        _, edges = _parse([_doc("src/mod.py", [
            _sym(sym_a, docs=["def fn_a():"], refs=[sym_b]),
            _sym(sym_b, docs=["def fn_b():"]),
        ])])
        assert len(edges) == 1

    def test_same_external_symbol_referenced_by_two_fns_creates_two_edges(self):
        """One stub node but two distinct call edges — the caller count is N, not 1."""
        sym_a = "scip-python python myproject 1.0 src/mod.py:fn_a()."
        sym_b = "scip-python python myproject 1.0 src/mod.py:fn_b()."
        _, edges = _parse([_doc("src/mod.py", [
            _sym(sym_a, docs=["def fn_a():"], refs=[PY_NUMPY]),
            _sym(sym_b, docs=["def fn_b():"], refs=[PY_NUMPY]),
        ])])
        assert len(edges) == 2


# ── Parse robustness ──────────────────────────────────────────────────────────

class TestParseRobustness:
    def test_invalid_json_raises_value_error(self):
        with pytest.raises(ValueError, match="Invalid SCIP JSON"):
            ScipImporter().parse("{ not valid json }")

    def test_empty_documents_returns_empty_results(self):
        nodes, edges = _parse([])
        assert nodes == []
        assert edges == []

    def test_empty_symbols_list_returns_no_nodes(self):
        nodes, edges = _parse([_doc("src/mod.py", [])])
        assert nodes == []
        assert edges == []

    def test_raw_json_string_accepted(self):
        """parse() accepts raw JSON content, not just file paths."""
        raw = json.dumps({"documents": [_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"])
        ])]})
        nodes, _ = ScipImporter().parse(raw)
        assert len(nodes) == 1

    def test_symbol_with_no_docs_still_produces_node(self):
        """A symbol that has no documentation array is still indexed."""
        nodes, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL)])])
        assert len([n for n in nodes if not n.is_external]) == 1

    def test_symbol_with_empty_docs_still_produces_node(self):
        nodes, _ = _parse([_doc("src/mod.py", [_sym(PY_INTERNAL, docs=[])])])
        assert len([n for n in nodes if not n.is_external]) == 1

    def test_symbol_with_blank_doc_lines_skipped_gracefully(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["", "  ", "def my_fn():"])
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert "my_fn" in node.signature


# ── Signature normalisation ───────────────────────────────────────────────────

class TestNormaliseExternalSignature:
    """
    Tests for _normalise_external_signature() — tested directly because this
    is the core logic that determines what callers see in get_callees,
    check_dependency, and fingerprint diffs.
    """

    def test_simple_function_produces_dotted_call(self):
        assert _normalise_external_signature(PY_NUMPY) == "numpy.array(...)"

    def test_top_level_function_from_requests(self):
        assert _normalise_external_signature(PY_REQUESTS) == "requests.get(...)"

    def test_class_method_includes_class_name(self):
        assert _normalise_external_signature(PY_SESSION_GET) == "requests.Session.get(...)"

    def test_typescript_npm_package(self):
        assert _normalise_external_signature(TS_USE_STATE) == "react.useState(...)"

    def test_non_callable_symbol_has_no_parens(self):
        non_callable = "scip-python python pandas 2.0 pandas/`DataFrame`."
        result = _normalise_external_signature(non_callable)
        assert "(...)" not in result
        assert result == "pandas.DataFrame"

    def test_raw_scip_syntax_not_present_in_output(self):
        result = _normalise_external_signature(PY_SESSION_GET)
        assert "scip-python" not in result
        assert "`" not in result
        assert "#" not in result

    def test_deeply_nested_method_uses_class_and_method(self):
        sym = "scip-python python fastapi 0.100 fastapi/`FastAPI`#`include_router`()."
        assert _normalise_external_signature(sym) == "fastapi.FastAPI.include_router(...)"

    def test_stdlib_dotted_path(self):
        sym = "scip-python python os 3.11 os/`path`#`join`()."
        assert _normalise_external_signature(sym) == "os.path.join(...)"


# ── Project root handling ─────────────────────────────────────────────────────

class TestProjectRoot:
    def test_without_root_file_path_is_relative(self):
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"])
        ])])
        node = next(n for n in nodes if not n.is_external)
        assert node.file == "src/mod.py"

    def test_with_root_file_path_is_absolute(self):
        nodes, _ = _parse(
            [_doc("src/mod.py", [_sym(PY_INTERNAL, docs=["def my_fn():"])])],
            root="/workspace/myproject",
        )
        node = next(n for n in nodes if not n.is_external)
        assert node.file == "/workspace/myproject/src/mod.py"

    def test_root_trailing_slash_handled(self):
        nodes, _ = _parse(
            [_doc("src/mod.py", [_sym(PY_INTERNAL, docs=["def my_fn():"])])],
            root="/workspace/myproject/",
        )
        node = next(n for n in nodes if not n.is_external)
        assert "//" not in node.file

    def test_external_stub_file_path_is_library_placeholder(self):
        """External stubs have a <library> placeholder, not a real path."""
        nodes, _ = _parse([_doc("src/mod.py", [
            _sym(PY_INTERNAL, docs=["def my_fn():"], refs=[PY_NUMPY])
        ])])
        stub = next(n for n in nodes if n.is_external)
        assert stub.file == "<numpy>"


# ── _scip_sym_to_node_id ─────────────────────────────────────────────────────

class TestScipSymToNodeId:
    """
    _scip_sym_to_node_id converts a SCIP internal symbol to a tree-sitter
    compatible node ID so get_callers() JOIN succeeds.

    Tree-sitter format: module.ClassName.method  or  module.function_name
    SCIP format:        scip-python python pkg ver src/foo.py:ClassName#method().
    """

    def test_module_level_function(self):
        """scip ...src/mod.py:my_fn(). → src.mod.my_fn"""
        sym = "scip-python python myproject 1.0 src/mod.py:my_fn()."
        assert _scip_sym_to_node_id(sym, "src.mod") == "src.mod.my_fn"

    def test_class_method_includes_class_name(self):
        """scip ...src/indexer.py:Indexer#index_project(). → src.indexer.Indexer.index_project"""
        sym = "scip-python python myproject 1.0 src/indexer.py:Indexer#index_project()."
        assert _scip_sym_to_node_id(sym, "src.indexer") == "src.indexer.Indexer.index_project"

    def test_backticks_stripped(self):
        """Backtick-quoted names produce clean output without backticks."""
        sym = "scip-python python myproject 1.0 src/mod.py:`my_fn`()."
        assert _scip_sym_to_node_id(sym, "src.mod") == "src.mod.my_fn"

    def test_class_method_backtick_names(self):
        sym = "scip-python python myproject 1.0 src/mod.py:`MyClass`#`method`()."
        assert _scip_sym_to_node_id(sym, "src.mod") == "src.mod.MyClass.method"

    def test_file_module_always_used_as_prefix(self):
        """The module prefix comes from the calling context, not the SCIP path."""
        sym = "scip-python python myproject 1.0 src/server.py:index_project()."
        result = _scip_sym_to_node_id(sym, "src.server")
        assert result.startswith("src.server.")

    def test_no_scip_package_hash_in_output(self):
        """The version / hash portion of a SCIP symbol must not appear in the ID."""
        sym = "scip-python python scopenos c93d2e87879ab90d3d1afce546288ba974754e14 src/server.py:index_project()."
        result = _scip_sym_to_node_id(sym, "src.server")
        assert "c93d2e87879ab90d3d1afce546288ba974754e14" not in result
        assert "scip-python" not in result


# ── Binary parser edge filtering ──────────────────────────────────────────────

class TestBinaryParserEdgeFiltering:
    """
    The binary SCIP parser (_parse_binary) should produce tree-sitter-format
    caller_ids on edges and must NOT emit internal→internal edges (tree-sitter
    already captures those; duplicates break get_callers() call counts).

    We build a minimal binary SCIP Index via scip_pb2 to exercise this path.
    """

    @pytest.fixture
    def binary_index_one_external(self):
        """One internal function calling one external library function."""
        from src import scip_pb2
        index = scip_pb2.Index()
        doc = index.documents.add()
        doc.relative_path = "src/mod.py"

        internal_sym = "scip-python python myproject 1.0 src/mod.py:my_fn()."
        external_sym = "scip-python python numpy 1.23.5 numpy/`array`()."

        # Symbol info for internal function
        si = doc.symbols.add()
        si.symbol = internal_sym

        # Occurrence: DEFINITION of my_fn at line 1
        occ_def = doc.occurrences.add()
        occ_def.symbol = internal_sym
        occ_def.symbol_roles = 1   # DEFINITION
        occ_def.range.extend([1, 0, 1, 5])

        # Occurrence: READ_ACCESS (call) to numpy.array inside my_fn
        occ_ref = doc.occurrences.add()
        occ_ref.symbol = external_sym
        occ_ref.symbol_roles = 8   # READ_ACCESS
        occ_ref.range.extend([2, 0, 2, 5])

        return index.SerializeToString()

    @pytest.fixture
    def binary_index_two_internal(self):
        """Two internal functions where one calls the other."""
        from src import scip_pb2
        index = scip_pb2.Index()
        doc = index.documents.add()
        doc.relative_path = "src/mod.py"

        sym_a = "scip-python python myproject 1.0 src/mod.py:fn_a()."
        sym_b = "scip-python python myproject 1.0 src/mod.py:fn_b()."

        for sym in (sym_a, sym_b):
            si = doc.symbols.add()
            si.symbol = sym

        # fn_a definition
        occ_a_def = doc.occurrences.add()
        occ_a_def.symbol = sym_a
        occ_a_def.symbol_roles = 1
        occ_a_def.range.extend([1, 0, 1, 4])

        # fn_a reads fn_b (internal call)
        occ_a_ref = doc.occurrences.add()
        occ_a_ref.symbol = sym_b
        occ_a_ref.symbol_roles = 8
        occ_a_ref.range.extend([2, 0, 2, 4])

        # fn_b definition
        occ_b_def = doc.occurrences.add()
        occ_b_def.symbol = sym_b
        occ_b_def.symbol_roles = 1
        occ_b_def.range.extend([5, 0, 5, 4])

        return index.SerializeToString()

    def test_external_ref_creates_edge(self, tmp_path, binary_index_one_external):
        """Binary parser: internal→external call produces a reference edge."""
        scip_file = tmp_path / "index.scip"
        scip_file.write_bytes(binary_index_one_external)
        _, edges = ScipImporter().parse(str(scip_file))
        assert len(edges) == 1
        assert edges[0].edge_type == "reference"

    def test_external_ref_caller_id_is_tree_sitter_format(self, tmp_path, binary_index_one_external):
        """caller_id must match tree-sitter format (module.function) not SCIP hash format."""
        scip_file = tmp_path / "index.scip"
        scip_file.write_bytes(binary_index_one_external)
        _, edges = ScipImporter().parse(str(scip_file))
        caller = edges[0].caller_id
        # Must be "src.mod.my_fn", NOT "src.mod.scip-python_python_myproject_..."
        assert caller == "src.mod.my_fn"
        assert "scip-python" not in caller

    def test_internal_to_internal_does_not_create_edge(self, tmp_path, binary_index_two_internal):
        """Binary parser must NOT emit internal→internal edges; tree-sitter handles those."""
        scip_file = tmp_path / "index.scip"
        scip_file.write_bytes(binary_index_two_internal)
        _, edges = ScipImporter().parse(str(scip_file))
        assert edges == []

    def test_external_stub_node_is_created(self, tmp_path, binary_index_one_external):
        """External library functions must appear as is_external=True nodes."""
        scip_file = tmp_path / "index.scip"
        scip_file.write_bytes(binary_index_one_external)
        nodes, _ = ScipImporter().parse(str(scip_file))
        external = [n for n in nodes if n.is_external]
        assert len(external) == 1
        assert external[0].name == "array"
