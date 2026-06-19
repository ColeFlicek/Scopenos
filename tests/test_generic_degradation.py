"""
Tests for feature behavior when operating on generic-parsed nodes.

Two behaviors under test:
1. _async_signal uses is_async field (not Python-only "async def" regex)
   so Kotlin suspend fun / Swift async func get correct guidance.
2. _check_async skips when all existing nodes are structural_layer='generic'
   to prevent false "module is 0% async" positives after re-indexing.
"""
import pytest
from src.guidance import _async_signal
from src.validate import _check_async


# ── Behavior 1: async signal uses is_async field ──────────────────────────────

class TestAsyncSignalUsesIsAsyncField:
    def test_no_signal_for_all_sync_python(self):
        results = [
            {"id": "m.f1", "name": "fetch", "signature": "def fetch(url):", "module": "m"},
            {"id": "m.f2", "name": "parse", "signature": "def parse(data):", "module": "m"},
        ]
        assert _async_signal(results) is None

    def test_fires_for_python_async_def(self):
        results = [
            {"id": "m.f1", "name": "fetch", "signature": "async def fetch(url):", "module": "m"},
            {"id": "m.f2", "name": "parse", "signature": "async def parse(data):", "module": "m"},
        ]
        assert _async_signal(results) is not None

    def test_fires_for_is_async_true_regardless_of_signature(self):
        # Kotlin suspend fun — no "async def" in signature, but is_async=True
        results = [
            {"id": "m.f1", "name": "fetchData",
             "signature": "suspend fun fetchData(url: String): Data",
             "module": "m", "is_async": 1},
            {"id": "m.f2", "name": "processData",
             "signature": "suspend fun processData(data: Data): Result",
             "module": "m", "is_async": 1},
        ]
        signal = _async_signal(results)
        assert signal is not None, (
            "Kotlin suspend fun module should produce an async signal. "
            "_async_signal must use is_async field, not just 'async def' regex."
        )
        assert "async" in signal.lower()

    def test_fires_for_swift_async_func(self):
        results = [
            {"id": "m.f1", "name": "Fetcher.fetchData",
             "signature": "func fetchData(url: String) async -> Data",
             "module": "m", "is_async": 1},
            {"id": "m.f2", "name": "Fetcher.loadImage",
             "signature": "func loadImage(url: String) async -> Image",
             "module": "m", "is_async": 1},
        ]
        signal = _async_signal(results)
        assert signal is not None

    def test_no_signal_when_is_async_false_and_no_async_def(self):
        # Generic-parsed Bash/Lua — is_async=False, no "async def"
        results = [
            {"id": "m.f1", "name": "greet",
             "signature": "function greet(name)", "module": "m", "is_async": 0},
            {"id": "m.f2", "name": "format",
             "signature": "function format(s)", "module": "m", "is_async": 0},
        ]
        assert _async_signal(results) is None

    def test_mixed_async_detected(self):
        results = [
            {"id": "m.f1", "name": "fetchData",
             "signature": "suspend fun fetchData(): Data", "module": "m", "is_async": 1},
            {"id": "m.f2", "name": "buildUrl",
             "signature": "fun buildUrl(path: String): String", "module": "m", "is_async": 0},
            {"id": "m.f3", "name": "parseData",
             "signature": "fun parseData(s: String): Data", "module": "m", "is_async": 0},
        ]
        signal = _async_signal(results)
        # 1/3 async — mixed signal, not all-async or all-sync
        assert signal is not None
        assert "mixed" in signal.lower() or "async" in signal.lower()


# ── Behavior 2: async check skips generic-layer modules ───────────────────────

class TestCheckAsyncSkipsGenericModules:
    def test_skips_when_all_existing_are_generic(self):
        # Simulates: Kotlin file indexed before precision parser, all is_async=False
        existing = [
            {"name": "fn1", "is_async": False, "structural_layer": "generic"},
            {"name": "fn2", "is_async": False, "structural_layer": "generic"},
            {"name": "fn3", "is_async": False, "structural_layer": "generic"},
        ]
        # New code with async — would be a false positive without the skip
        proposed = [{"name": "suspend_fn", "is_async": True, "body": "", "signature": "suspend fun f()"}]
        result = _check_async(proposed, existing)
        assert result is None, (
            "_check_async should skip when all existing nodes are structural_layer='generic'. "
            "The 0%% async ratio is a data absence artifact, not a real convention."
        )

    def test_still_fires_for_all_precision_sync_module(self):
        existing = [
            {"name": "fn1", "is_async": False, "structural_layer": "precision"},
            {"name": "fn2", "is_async": False, "structural_layer": "precision"},
        ]
        proposed = [{"name": "async_fn", "is_async": True, "body": "", "signature": "async def f():"}]
        result = _check_async(proposed, existing)
        assert result is not None
        assert result.check == "async"

    def test_fires_for_mixed_precision_and_generic(self):
        # At least one precision node → check runs (partial data is better than none)
        existing = [
            {"name": "fn1", "is_async": False, "structural_layer": "precision"},
            {"name": "fn2", "is_async": False, "structural_layer": "generic"},
        ]
        proposed = [{"name": "async_fn", "is_async": True, "body": "", "signature": "async def f():"}]
        result = _check_async(proposed, existing)
        assert result is not None

    def test_no_false_positive_when_generic_module_is_all_sync_proposed(self):
        # All-generic module, proposed also has no async — should never warn
        existing = [
            {"name": "greet", "is_async": False, "structural_layer": "generic"},
        ]
        proposed = [{"name": "add", "is_async": False, "body": "", "signature": "function add(a, b)"}]
        result = _check_async(proposed, existing)
        assert result is None

    def test_skips_when_structural_layer_missing_from_existing(self):
        # Old DB nodes before structural_layer column existed — treat as precision
        existing = [
            {"name": "fn1", "is_async": False},  # no structural_layer key
            {"name": "fn2", "is_async": False},
        ]
        proposed = [{"name": "async_fn", "is_async": True, "body": "", "signature": "async def f():"}]
        # Should still run the check (absence of structural_layer = assume precision)
        result = _check_async(proposed, existing)
        assert result is not None
