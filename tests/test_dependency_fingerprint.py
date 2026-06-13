"""
Tests for DependencyFingerprinter, DependencyFingerprint, FingerprintDiff,
and resolve_versions.

All tests use plain Python — no database, no async, no file I/O except where
resolve_versions behaviour under specific lockfile content is the subject.
Tests are specifications: each one maps to a production scenario where the
wrong behaviour causes a missed failure signal.
"""
import pytest
from src.dependency_fingerprint import (
    DependencyFingerprint,
    DependencyFingerprinter,
    FingerprintDiff,
    resolve_versions,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _lib(name: str, *symbols: tuple[str, str], caller_count: int = 1) -> dict:
    """Build a minimal library entry matching list_external_dependencies() output."""
    syms = [
        {"id": f"external.{name}.{sym}", "signature": sig, "caller_count": caller_count}
        for sym, sig in symbols
    ]
    return {"library": name, "symbol_count": len(syms), "symbols": syms}


def _fp(
    project_id: str = "proj",
    *lib_entries: dict,
    project_path: str = "",
) -> DependencyFingerprint:
    """Compute a fingerprint from lib entries — shorthand for tests."""
    return DependencyFingerprinter().compute(project_id, list(lib_entries), project_path)


# ── compute() — structure ─────────────────────────────────────────────────────

class TestComputeStructure:
    def test_project_id_is_preserved(self):
        fp = _fp("myproject", _lib("numpy", ("array", "numpy.array(...)")))
        assert fp.project_id == "myproject"

    def test_total_libraries_counts_libraries(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")), _lib("requests", ("get", "sig")))
        assert fp.total_libraries == 2

    def test_total_external_symbols_sums_across_libraries(self):
        fp = _fp("p",
            _lib("numpy", ("array", "sig"), ("sum", "sig")),
            _lib("requests", ("get", "sig")),
        )
        assert fp.total_external_symbols == 3

    def test_library_names_present_in_libraries_dict(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")), _lib("requests", ("get", "sig")))
        assert "numpy" in fp.libraries
        assert "requests" in fp.libraries

    def test_empty_deps_returns_zero_counts(self):
        fp = _fp("p")
        assert fp.total_libraries == 0
        assert fp.total_external_symbols == 0
        assert fp.libraries == {}

    def test_fingerprint_hash_is_nonempty_string(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")))
        assert isinstance(fp.fingerprint_hash, str)
        assert len(fp.fingerprint_hash) > 0

    def test_version_is_unknown_when_no_lockfile(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")))
        assert fp.libraries["numpy"]["version"] == "unknown"

    def test_captured_at_is_set(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")))
        assert fp.captured_at  # non-empty ISO timestamp


# ── compute() — hash stability ────────────────────────────────────────────────

class TestComputeHashStability:
    def test_same_input_produces_same_hash(self):
        deps = [_lib("numpy", ("array", "numpy.array(...)"))]
        h1 = DependencyFingerprinter().compute("p", deps).fingerprint_hash
        h2 = DependencyFingerprinter().compute("p", deps).fingerprint_hash
        assert h1 == h2

    def test_different_symbols_produce_different_hashes(self):
        fp1 = _fp("p", _lib("numpy", ("array", "sig")))
        fp2 = _fp("p", _lib("numpy", ("zeros", "sig")))
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_different_signatures_produce_different_hashes(self):
        fp1 = _fp("p", _lib("numpy", ("array", "old_sig")))
        fp2 = _fp("p", _lib("numpy", ("array", "new_sig")))
        assert fp1.fingerprint_hash != fp2.fingerprint_hash

    def test_symbol_order_does_not_affect_hash(self):
        """Hash must be stable regardless of dict iteration order."""
        deps_a = [{"library": "numpy", "symbol_count": 2, "symbols": [
            {"id": "external.numpy.array", "signature": "sig_a", "caller_count": 1},
            {"id": "external.numpy.zeros", "signature": "sig_b", "caller_count": 1},
        ]}]
        deps_b = [{"library": "numpy", "symbol_count": 2, "symbols": [
            {"id": "external.numpy.zeros", "signature": "sig_b", "caller_count": 1},
            {"id": "external.numpy.array", "signature": "sig_a", "caller_count": 1},
        ]}]
        h1 = DependencyFingerprinter().compute("p", deps_a).fingerprint_hash
        h2 = DependencyFingerprinter().compute("p", deps_b).fingerprint_hash
        assert h1 == h2

    def test_version_bump_changes_hash_even_when_symbols_identical(self):
        """
        Core invariant: a library that upgrades internally — without changing
        its symbol surface — still changes the fingerprint. Without this, a
        silent breaking upgrade would produce no fingerprint diff at all.
        Requires a lockfile to supply the version.
        """
        deps = [_lib("requests", ("get", "requests.get(...)"))]
        fp1 = DependencyFingerprinter().compute("p", deps)
        # Inject versions manually by patching the library dict post-hoc,
        # then re-hash to simulate what compute() does with a real lockfile.
        # We test the hash directly by calling compute() twice with different
        # versions resolved — simulated via a tmp lockfile.
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            req = os.path.join(d, "requirements.txt")
            with open(req, "w") as f:
                f.write("requests==2.28.0\n")
            fp_old = DependencyFingerprinter().compute("p", deps, project_path=d)

            with open(req, "w") as f:
                f.write("requests==2.31.0\n")
            fp_new = DependencyFingerprinter().compute("p", deps, project_path=d)

        assert fp_old.fingerprint_hash != fp_new.fingerprint_hash


# ── diff() — no changes ───────────────────────────────────────────────────────

class TestDiffNoChanges:
    def test_identical_fingerprints_produce_empty_diff(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")))
        diff = DependencyFingerprinter().diff(fp, fp)
        assert diff.removed_symbols == []
        assert diff.added_symbols == []
        assert diff.changed_symbols == []

    def test_has_changes_is_false_for_empty_diff(self):
        fp = _fp("p", _lib("numpy", ("array", "sig")))
        diff = DependencyFingerprinter().diff(fp, fp)
        assert diff.has_changes is False


# ── diff() — removed symbols (primary failure signal) ────────────────────────

class TestDiffRemovedSymbols:
    def test_removed_symbol_appears_in_removed_symbols(self):
        """
        This is the signal for runtime ImportError / AttributeError.
        A symbol that existed before is gone — something in the environment changed.
        """
        old = _fp("p", _lib("requests", ("get", "sig"), ("post", "sig")))
        new = _fp("p", _lib("requests", ("get", "sig")))  # post removed
        diff = DependencyFingerprinter().diff(old, new)
        ids = [s["id"] for s in diff.removed_symbols]
        assert "external.requests.post" in ids

    def test_removed_symbol_not_in_added_symbols(self):
        old = _fp("p", _lib("requests", ("get", "sig"), ("post", "sig")))
        new = _fp("p", _lib("requests", ("get", "sig")))
        diff = DependencyFingerprinter().diff(old, new)
        added_ids = [s["id"] for s in diff.added_symbols]
        assert "external.requests.post" not in added_ids

    def test_removed_symbol_carries_library_name(self):
        old = _fp("p", _lib("requests", ("post", "sig")))
        new = _fp("p")
        diff = DependencyFingerprinter().diff(old, new)
        assert diff.removed_symbols[0]["library"] == "requests"

    def test_entire_library_removed_all_symbols_in_removed(self):
        old = _fp("p",
            _lib("requests", ("get", "sig"), ("post", "sig")),
            _lib("numpy", ("array", "sig")),
        )
        new = _fp("p", _lib("numpy", ("array", "sig")))  # requests gone
        diff = DependencyFingerprinter().diff(old, new)
        removed_ids = {s["id"] for s in diff.removed_symbols}
        assert "external.requests.get" in removed_ids
        assert "external.requests.post" in removed_ids

    def test_has_changes_is_true_when_symbol_removed(self):
        old = _fp("p", _lib("requests", ("get", "sig"), ("post", "sig")))
        new = _fp("p", _lib("requests", ("get", "sig")))
        diff = DependencyFingerprinter().diff(old, new)
        assert diff.has_changes is True


# ── diff() — added symbols ────────────────────────────────────────────────────

class TestDiffAddedSymbols:
    def test_new_symbol_appears_in_added_symbols(self):
        old = _fp("p", _lib("requests", ("get", "sig")))
        new = _fp("p", _lib("requests", ("get", "sig"), ("post", "sig")))
        diff = DependencyFingerprinter().diff(old, new)
        ids = [s["id"] for s in diff.added_symbols]
        assert "external.requests.post" in ids

    def test_added_symbol_not_in_removed_symbols(self):
        old = _fp("p", _lib("requests", ("get", "sig")))
        new = _fp("p", _lib("requests", ("get", "sig"), ("post", "sig")))
        diff = DependencyFingerprinter().diff(old, new)
        removed_ids = [s["id"] for s in diff.removed_symbols]
        assert "external.requests.post" not in removed_ids

    def test_entirely_new_library_all_symbols_in_added(self):
        old = _fp("p", _lib("numpy", ("array", "sig")))
        new = _fp("p", _lib("numpy", ("array", "sig")), _lib("pandas", ("DataFrame", "sig")))
        diff = DependencyFingerprinter().diff(old, new)
        added_ids = {s["id"] for s in diff.added_symbols}
        assert "external.pandas.DataFrame" in added_ids


# ── diff() — changed signatures ───────────────────────────────────────────────

class TestDiffChangedSignatures:
    def test_changed_signature_appears_in_changed_symbols(self):
        old = _fp("p", _lib("numpy", ("array", "old_sig")))
        new = _fp("p", _lib("numpy", ("array", "new_sig")))
        diff = DependencyFingerprinter().diff(old, new)
        ids = [s["id"] for s in diff.changed_symbols]
        assert "external.numpy.array" in ids

    def test_changed_symbol_carries_old_and_new_signature(self):
        old = _fp("p", _lib("numpy", ("array", "old_sig")))
        new = _fp("p", _lib("numpy", ("array", "new_sig")))
        diff = DependencyFingerprinter().diff(old, new)
        changed = diff.changed_symbols[0]
        assert changed["old_signature"] == "old_sig"
        assert changed["new_signature"] == "new_sig"

    def test_unchanged_symbol_not_in_changed_symbols(self):
        fp = _fp("p", _lib("numpy", ("array", "same_sig")))
        diff = DependencyFingerprinter().diff(fp, fp)
        assert diff.changed_symbols == []


# ── diff() — version changes ──────────────────────────────────────────────────

class TestDiffVersionChanges:
    def _fp_with_version(self, version: str) -> DependencyFingerprint:
        """Build a fingerprint with a known version via a tmp requirements.txt."""
        import tempfile, os
        deps = [_lib("requests", ("get", "requests.get(...)"))]
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "requirements.txt"), "w") as f:
                f.write(f"requests=={version}\n")
            return DependencyFingerprinter().compute("p", deps, project_path=d)

    def test_version_bump_appears_in_version_changes(self):
        old = self._fp_with_version("2.28.0")
        new = self._fp_with_version("2.31.0")
        diff = DependencyFingerprinter().diff(old, new)
        libs = [v["library"] for v in diff.version_changes]
        assert "requests" in libs

    def test_version_change_carries_old_and_new_version(self):
        old = self._fp_with_version("2.28.0")
        new = self._fp_with_version("2.31.0")
        diff = DependencyFingerprinter().diff(old, new)
        change = next(v for v in diff.version_changes if v["library"] == "requests")
        assert change["old_version"] == "2.28.0"
        assert change["new_version"] == "2.31.0"

    def test_unknown_versions_excluded_from_version_changes(self):
        """
        A library with no lockfile entry has version 'unknown' on both sides.
        This must not appear in version_changes — unknown→unknown is not a
        detectable change, and reporting it would be noise.
        """
        fp1 = _fp("p", _lib("requests", ("get", "sig")))  # no lockfile → unknown
        fp2 = _fp("p", _lib("requests", ("get", "sig")))
        diff = DependencyFingerprinter().diff(fp1, fp2)
        assert diff.version_changes == []

    def test_has_changes_true_when_only_version_changed(self):
        old = self._fp_with_version("2.28.0")
        new = self._fp_with_version("2.31.0")
        diff = DependencyFingerprinter().diff(old, new)
        assert diff.has_changes is True


# ── resolve_versions() — requirements.txt ────────────────────────────────────

class TestResolveVersionsRequirementsTxt:
    def _write(self, tmp_path, content: str) -> str:
        req = tmp_path / "requirements.txt"
        req.write_text(content)
        return str(tmp_path)

    def test_exact_pin_returns_version(self, tmp_path):
        path = self._write(tmp_path, "requests==2.28.0\nnumpy==1.24.3\n")
        versions = resolve_versions(path, {"requests", "numpy"})
        assert versions["requests"] == "2.28.0"
        assert versions["numpy"] == "1.24.3"

    def test_compatible_release_returns_version(self, tmp_path):
        path = self._write(tmp_path, "requests~=2.28.0\n")
        versions = resolve_versions(path, {"requests"})
        assert versions["requests"] == "2.28.0"

    def test_range_pin_not_returned(self, tmp_path):
        """>=1.0 is not an exact version — omit rather than guess."""
        path = self._write(tmp_path, "requests>=2.28.0\n")
        versions = resolve_versions(path, {"requests"})
        assert "requests" not in versions

    def test_comments_and_blank_lines_ignored(self, tmp_path):
        path = self._write(tmp_path, "# comment\n\nrequests==2.28.0\n")
        versions = resolve_versions(path, {"requests"})
        assert versions["requests"] == "2.28.0"

    def test_library_not_in_file_not_in_result(self, tmp_path):
        path = self._write(tmp_path, "numpy==1.24.3\n")
        versions = resolve_versions(path, {"requests"})
        assert "requests" not in versions

    def test_case_insensitive_library_matching(self, tmp_path):
        """PEP 503: Requests and requests are the same package."""
        path = self._write(tmp_path, "Requests==2.28.0\n")
        versions = resolve_versions(path, {"requests"})
        assert versions.get("requests") == "2.28.0"

    def test_empty_project_path_returns_empty(self):
        versions = resolve_versions("", {"requests"})
        assert versions == {}

    def test_missing_lockfile_returns_empty(self, tmp_path):
        versions = resolve_versions(str(tmp_path), {"requests"})
        assert versions == {}


# ── resolve_versions() — poetry.lock ─────────────────────────────────────────

class TestResolveVersionsPoetryLock:
    def _write(self, tmp_path, content: str) -> str:
        (tmp_path / "poetry.lock").write_text(content)
        return str(tmp_path)

    def test_parses_package_block(self, tmp_path):
        content = (
            '[[package]]\nname = "requests"\nversion = "2.28.0"\n'
            'description = "HTTP library"\n'
        )
        path = self._write(tmp_path, content)
        versions = resolve_versions(path, {"requests"})
        assert versions["requests"] == "2.28.0"

    def test_multiple_packages_in_lock(self, tmp_path):
        content = (
            '[[package]]\nname = "requests"\nversion = "2.28.0"\n\n'
            '[[package]]\nname = "numpy"\nversion = "1.24.3"\n'
        )
        path = self._write(tmp_path, content)
        versions = resolve_versions(path, {"requests", "numpy"})
        assert versions["requests"] == "2.28.0"
        assert versions["numpy"] == "1.24.3"

    def test_unneeded_packages_not_in_result(self, tmp_path):
        content = '[[package]]\nname = "pytest"\nversion = "8.0.0"\n'
        path = self._write(tmp_path, content)
        versions = resolve_versions(path, {"requests"})
        assert "pytest" not in versions


# ── Mixed real-world scenario ─────────────────────────────────────────────────

class TestRealWorldScenario:
    def test_upgrade_plus_api_change_plus_new_lib(self, tmp_path):
        """
        Simulates a realistic dependency event between two deployments:
        - requests upgraded 2.28→2.31 (version change, no symbol change)
        - requests.post signature changed (breaking API change)
        - pandas added as new dependency
        - a custom internal lib's symbol disappeared (removed_symbols)

        All four signals surface in the diff independently.
        """
        (tmp_path / "requirements.txt").write_text("requests==2.28.0\n")
        old = DependencyFingerprinter().compute("p", [
            _lib("requests", ("get", "requests.get(url)"), ("post", "requests.post(url, data)")),
            _lib("mylib", ("helper", "mylib.helper()")),
        ], project_path=str(tmp_path))

        (tmp_path / "requirements.txt").write_text("requests==2.31.0\npandas==2.0.0\n")
        new = DependencyFingerprinter().compute("p", [
            _lib("requests", ("get", "requests.get(url)"), ("post", "requests.post(url, json)")),
            _lib("pandas", ("DataFrame", "pandas.DataFrame()")),
            # mylib.helper gone
        ], project_path=str(tmp_path))

        diff = DependencyFingerprinter().diff(old, new)

        assert any(v["library"] == "requests" for v in diff.version_changes)
        assert any(s["id"] == "external.requests.post" for s in diff.changed_symbols)
        assert any(s["id"] == "external.pandas.DataFrame" for s in diff.added_symbols)
        assert any(s["id"] == "external.mylib.helper" for s in diff.removed_symbols)
        assert diff.has_changes is True
