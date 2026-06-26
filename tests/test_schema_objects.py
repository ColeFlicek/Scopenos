"""
Tests for schema object extraction and cardinality labeling (src/schema_objects.py).

Public interfaces under test:
  - _cardinality_from_row_count(count) → str
  - _KNOWN_CARDINALITY dict
  - SchemaObject dataclass
  - _table_description(table, columns, references, referenced_by, cardinality, row_count) → str
  - _class_description(class_name, methods, docstring, cardinality) → str

No database, no async I/O. All tested functions are pure.
"""
import pytest
from src.schema_objects import (
    SchemaObject,
    _cardinality_from_row_count,
    _class_description,
    _table_description,
    _KNOWN_CARDINALITY,
)


# ── Cardinality from row count ────────────────────────────────────────────────

class TestCardinalityFromRowCount:
    def test_below_ten_is_low(self):
        assert _cardinality_from_row_count(0) == "LOW"
        assert _cardinality_from_row_count(9) == "LOW"

    def test_ten_to_999_is_medium(self):
        assert _cardinality_from_row_count(10) == "MEDIUM"
        assert _cardinality_from_row_count(999) == "MEDIUM"

    def test_thousand_to_99999_is_high(self):
        assert _cardinality_from_row_count(1_000) == "HIGH"
        assert _cardinality_from_row_count(99_999) == "HIGH"

    def test_hundred_thousand_plus_is_unbounded(self):
        assert _cardinality_from_row_count(100_000) == "UNBOUNDED"
        assert _cardinality_from_row_count(10_000_000) == "UNBOUNDED"

    def test_boundary_at_ten(self):
        assert _cardinality_from_row_count(9) == "LOW"
        assert _cardinality_from_row_count(10) == "MEDIUM"

    def test_boundary_at_thousand(self):
        assert _cardinality_from_row_count(999) == "MEDIUM"
        assert _cardinality_from_row_count(1_000) == "HIGH"

    def test_boundary_at_hundred_thousand(self):
        assert _cardinality_from_row_count(99_999) == "HIGH"
        assert _cardinality_from_row_count(100_000) == "UNBOUNDED"


# ── Known cardinality table ───────────────────────────────────────────────────

class TestKnownCardinality:
    """Critical domain facts baked into _KNOWN_CARDINALITY must stay correct."""

    def test_nodes_table_is_high(self):
        assert _KNOWN_CARDINALITY["nodes"] == "HIGH"

    def test_edges_table_is_high(self):
        assert _KNOWN_CARDINALITY["edges"] == "HIGH"

    def test_projects_table_is_low(self):
        assert _KNOWN_CARDINALITY["projects"] == "LOW"

    def test_users_table_is_low(self):
        assert _KNOWN_CARDINALITY["users"] == "LOW"

    def test_python_list_is_high(self):
        assert _KNOWN_CARDINALITY["list"] == "HIGH"

    def test_python_dict_is_high(self):
        assert _KNOWN_CARDINALITY["dict"] == "HIGH"

    def test_python_str_is_scalar(self):
        assert _KNOWN_CARDINALITY["str"] == "SCALAR"

    def test_python_bool_is_scalar(self):
        assert _KNOWN_CARDINALITY["bool"] == "SCALAR"

    def test_function_embeddings_is_high(self):
        """Embeddings table grows with every indexed function — must be HIGH."""
        assert _KNOWN_CARDINALITY["function_embeddings"] == "HIGH"


# ── Table description text ────────────────────────────────────────────────────

class TestTableDescription:
    """_table_description produces embedding-ready text for a DB table."""

    BASE_COLUMNS = [
        {"name": "id", "type": "integer"},
        {"name": "project_id", "type": "text"},
        {"name": "name", "type": "text"},
    ]

    def test_contains_table_name(self):
        text = _table_description("nodes", self.BASE_COLUMNS, [], [], "HIGH", 50000)
        assert "nodes" in text

    def test_contains_cardinality(self):
        text = _table_description("nodes", self.BASE_COLUMNS, [], [], "HIGH", 50000)
        assert "HIGH" in text

    def test_contains_column_names(self):
        text = _table_description("nodes", self.BASE_COLUMNS, [], [], "MEDIUM", None)
        assert "id" in text
        assert "project_id" in text

    def test_references_included_when_present(self):
        text = _table_description("edges", self.BASE_COLUMNS, ["nodes"], [], "HIGH", None)
        assert "nodes" in text

    def test_referenced_by_included_when_present(self):
        text = _table_description("projects", self.BASE_COLUMNS, [], ["nodes", "edges"], "LOW", 5)
        assert "nodes" in text or "edges" in text

    def test_row_count_included_when_known(self):
        text = _table_description("nodes", self.BASE_COLUMNS, [], [], "HIGH", 75000)
        assert "75000" in text

    def test_row_count_omitted_when_none(self):
        text = _table_description("nodes", self.BASE_COLUMNS, [], [], "HIGH", None)
        # Should not blow up and should still produce valid text
        assert "nodes" in text
        assert "HIGH" in text

    def test_no_references_section_when_empty(self):
        text = _table_description("users", self.BASE_COLUMNS, [], [], "LOW", 3)
        # Should not mention "References" with an empty list
        assert "References (many-to-one): " not in text


# ── Class description text ────────────────────────────────────────────────────

class TestClassDescription:
    """_class_description produces embedding-ready text for a Python class."""

    def test_contains_class_name(self):
        text = _class_description("CallGraphDB", ["create", "get_callers"], "", "LOW")
        assert "CallGraphDB" in text

    def test_contains_cardinality(self):
        text = _class_description("FunctionNode", [], "", "MEDIUM")
        assert "MEDIUM" in text

    def test_contains_method_names(self):
        text = _class_description("Indexer", ["index_project", "index_changes"], "", "LOW")
        assert "index_project" in text
        assert "index_changes" in text

    def test_docstring_included_when_present(self):
        doc = "Manages the call graph database connection pool."
        text = _class_description("CallGraphDB", [], doc, "LOW")
        assert "Manages" in text

    def test_empty_methods_no_methods_section(self):
        text = _class_description("MyClass", [], "", "SCALAR")
        # Should not crash and should still have the class name
        assert "MyClass" in text

    def test_long_docstring_truncated(self):
        long_doc = "x" * 500
        text = _class_description("MyClass", [], long_doc, "MEDIUM")
        # Should not include the full 500 chars
        assert len(text) < 1000


# ── SchemaObject dataclass ────────────────────────────────────────────────────

class TestSchemaObject:
    def test_construction_with_required_fields(self):
        obj = SchemaObject(
            name="nodes",
            source="db_table",
            project_id="scopenos",
            cardinality="HIGH",
            description="Database table: nodes",
        )
        assert obj.name == "nodes"
        assert obj.cardinality == "HIGH"
        assert obj.embedding is None  # not yet embedded

    def test_references_default_empty(self):
        obj = SchemaObject(
            name="edges", source="db_table", project_id="test",
            cardinality="HIGH", description="edges table",
        )
        assert obj.references == []
        assert obj.referenced_by == []

    def test_embedding_can_be_set(self):
        obj = SchemaObject(
            name="nodes", source="db_table", project_id="test",
            cardinality="HIGH", description="nodes",
        )
        obj.embedding = [0.1, 0.2, 0.3]
        assert obj.embedding == [0.1, 0.2, 0.3]
