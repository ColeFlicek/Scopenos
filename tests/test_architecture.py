"""
Architectural fitness functions — run in CI to catch abstraction breaches.

CONTRACT: Direct access to db._db or db._pool is restricted to two modules:
  - src/call_graph/storage.py  — owns all core Postgres tables
  - src/embeddings/embedder.py — owns the pgvector vec0 tables

Every other module that needs database data MUST go through a CallGraphDB
method. Reaching through the abstraction to call db._db.execute() from
analysis, detection, or route code violates the storage ownership boundary
and makes refactoring, testing, and schema changes harder.

WHY THIS CONTRACT EXISTS
  Performance.py and schema_objects.py previously queried nodes, edges,
  decisions, information_schema, and pg_class directly — identical to the
  breach that was fixed in EmbeddingStore (Change 2) and ContractManager
  (Change 3) in docs/architecture-deepening.md.

HOW TO FIX A VIOLATION
  Add a method to CallGraphDB in src/call_graph/storage.py and call it
  instead of using db._db or db._pool. The new method documents the query
  intent, keeps SQL in the right owner, and is mockable in tests.

EXCEPTIONS (documented, not hidden)
  schema_objects.py: owns schema_object_embeddings — access to that
  table via db._db is legitimate. The schema check below allows it.
"""
import re
from pathlib import Path

SRC = Path(__file__).parent.parent / "src"

# Pattern that catches direct raw-connection access
_RAW_ACCESS = re.compile(r"\._db\.execute\(|\._db\.executemany\(|\._pool\.acquire\(")

# Modules allowed to hold raw DB access — they own the tables they touch
_ALLOWED = {
    "call_graph/storage.py",   # owns all core tables
    "embeddings/embedder.py",  # owns pgvector vec0 tables
    "schema_objects.py",       # owns schema_object_embeddings (verified by separate test below)
}

# Known remaining violations — fix these before removing from this list.
# Each entry must have a comment explaining what it accesses and why it's deferred.
_KNOWN_VIOLATIONS = {
    "web/routes.py",   # accesses nodes, edges, decisions — needs count_* methods
                       # in CallGraphDB; deferred from the Candidate 5 refactor.
}

# schema_objects.py is a special case: it owns schema_object_embeddings and
# may access that table.  It must NOT access any other table via _db directly.
# Checked separately below.
_SCHEMA_OBJECTS_ALLOWED_PATTERN = re.compile(
    r"schema_object_embeddings"
)


class TestStorageAbstractionBoundary:
    """
    The storage abstraction boundary: only storage.py and embedder.py may
    call db._db / db._pool directly. All other modules must use CallGraphDB methods.
    """

    def test_no_raw_db_access_outside_allowed_modules(self):
        """No module outside the allowed list may access db._db or db._pool."""
        violations = []
        for py_file in SRC.rglob("*.py"):
            if py_file.name == "__init__.py":
                continue
            rel = py_file.relative_to(SRC).as_posix()
            if rel in _ALLOWED or rel in _KNOWN_VIOLATIONS:
                continue

            source = py_file.read_text()
            matches = _RAW_ACCESS.findall(source)
            if matches:
                violations.append(f"  {rel}: found {set(matches)}")

        assert not violations, (
            "\nRaw _db.execute / _pool.acquire access found outside allowed storage modules.\n"
            "Add a method to CallGraphDB instead of querying tables directly:\n"
            + "\n".join(violations)
            + "\n\nSee tests/test_architecture.py for the contract and how to fix."
        )

    def test_known_violations_still_exist(self):
        """Guard against the known-violations list going stale.

        If a known violation is fixed, it must be removed from _KNOWN_VIOLATIONS —
        otherwise the list becomes meaningless. This test fails if a 'known' file
        no longer contains any violations (meaning it was fixed but not cleaned up).
        """
        stale = []
        for rel in _KNOWN_VIOLATIONS:
            py_file = SRC / rel
            if not py_file.exists():
                continue
            source = py_file.read_text()
            if not _RAW_ACCESS.search(source):
                stale.append(f"  {rel}: no violations found — remove from _KNOWN_VIOLATIONS")

        assert not stale, (
            "\nThese files in _KNOWN_VIOLATIONS no longer have any violations.\n"
            "Remove them from the list so new violations in those files are caught:\n"
            + "\n".join(stale)
        )

    def test_schema_objects_only_accesses_its_own_table(self):
        """schema_objects.py may access db._db only for schema_object_embeddings.

        All Postgres system tables (information_schema, pg_class) and core
        CallGraphDB tables (nodes, edges, decisions) must be accessed via
        CallGraphDB methods, not db._db directly.
        """
        schema_file = SRC / "schema_objects.py"
        source = schema_file.read_text()

        # Find every db._db.execute / db._db.executemany occurrence and check
        # that the surrounding SQL only mentions schema_object_embeddings
        raw_calls = list(re.finditer(r"db\._db\.(execute|executemany)\(", source))
        violations = []
        for m in raw_calls:
            # Grab the next 200 chars to see what table is accessed
            context = source[m.start():m.start() + 200]
            if not _SCHEMA_OBJECTS_ALLOWED_PATTERN.search(context):
                line_no = source[:m.start()].count("\n") + 1
                violations.append(f"  line {line_no}: {context[:80].strip()!r}")

        assert not violations, (
            "\nschema_objects.py accesses tables other than schema_object_embeddings "
            "via db._db directly. Move these queries to CallGraphDB methods:\n"
            + "\n".join(violations)
        )
