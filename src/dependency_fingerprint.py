from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


# ── Version resolution ─────────────────────────────────────────────────────────

def _normalise(name: str) -> str:
    """PEP 503 canonical name: lowercase, collapse [-_.] to -."""
    return re.sub(r"[-_.]+", "-", name).lower()


def resolve_versions(project_path: str, library_names: set[str]) -> dict[str, str]:
    """
    Read the project's lockfile and return {library_name: version} for each
    library we care about. Tries lockfiles in priority order; stops at the
    first one that exists and yields at least one match.

    Returns an empty dict if no lockfile is found or none of the libraries
    appear in it — callers treat a missing version as unknown, not an error.
    """
    root = Path(project_path)
    canonical = {_normalise(n): n for n in library_names}

    for reader in (_read_poetry_lock, _read_pipfile_lock,
                   _read_requirements_txt, _read_package_lock_json):
        try:
            versions = reader(root, canonical)
        except Exception:
            continue
        if versions:
            return versions

    return {}


def _read_poetry_lock(root: Path, canonical: dict[str, str]) -> dict[str, str]:
    lock = root / "poetry.lock"
    if not lock.exists():
        return {}
    text = lock.read_text(encoding="utf-8", errors="replace")
    # Each package block: [[package]] … name = "…" … version = "…"
    result: dict[str, str] = {}
    for block in re.split(r"\[\[package\]\]", text)[1:]:
        name_m = re.search(r'name\s*=\s*"([^"]+)"', block)
        ver_m  = re.search(r'version\s*=\s*"([^"]+)"', block)
        if name_m and ver_m:
            key = _normalise(name_m.group(1))
            if key in canonical:
                result[canonical[key]] = ver_m.group(1)
    return result


def _read_pipfile_lock(root: Path, canonical: dict[str, str]) -> dict[str, str]:
    lock = root / "Pipfile.lock"
    if not lock.exists():
        return {}
    data = json.loads(lock.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    for section in ("default", "develop"):
        for pkg, info in data.get(section, {}).items():
            key = _normalise(pkg)
            if key in canonical:
                ver = info.get("version", "")
                result[canonical[key]] = ver.lstrip("=")
    return result


def _read_requirements_txt(root: Path, canonical: dict[str, str]) -> dict[str, str]:
    # Try requirements.txt, requirements/base.txt, requirements/prod.txt
    candidates = [
        root / "requirements.txt",
        root / "requirements" / "base.txt",
        root / "requirements" / "prod.txt",
    ]
    result: dict[str, str] = {}
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Accept pkg==1.2.3  or  pkg~=1.2  (exact and compatible pins)
            m = re.match(r"^([A-Za-z0-9_.-]+)\s*[=~]=\s*([^\s;#]+)", line)
            if m:
                key = _normalise(m.group(1))
                if key in canonical:
                    result[canonical[key]] = m.group(2)
    return result


def _read_package_lock_json(root: Path, canonical: dict[str, str]) -> dict[str, str]:
    lock = root / "package-lock.json"
    if not lock.exists():
        return {}
    data = json.loads(lock.read_text(encoding="utf-8"))
    result: dict[str, str] = {}
    # v2/v3 format: packages dict with "node_modules/pkg" keys
    for pkg_path, info in data.get("packages", {}).items():
        pkg = pkg_path.removeprefix("node_modules/")
        key = _normalise(pkg)
        if key in canonical:
            result[canonical[key]] = info.get("version", "unknown")
    return result


# ── Dataclasses ────────────────────────────────────────────────────────────────

@dataclass
class FingerprintDiff:
    removed_symbols: list[dict]  # [{id, library, signature}] — primary runtime failure signal
    added_symbols: list[dict]    # [{id, library, signature}]
    changed_symbols: list[dict]  # [{id, library, old_signature, new_signature}]
    version_changes: list[dict]  # [{library, old_version, new_version}]

    @property
    def has_changes(self) -> bool:
        return bool(
            self.removed_symbols or self.added_symbols
            or self.changed_symbols or self.version_changes
        )

    def to_dict(self) -> dict:
        return {
            "removed_symbols": self.removed_symbols,
            "added_symbols": self.added_symbols,
            "changed_symbols": self.changed_symbols,
            "version_changes": self.version_changes,
        }


@dataclass
class DependencyFingerprint:
    project_id: str
    captured_at: str
    fingerprint_hash: str
    total_libraries: int
    total_external_symbols: int
    libraries: dict[str, dict]  # library_name → {version, symbol_count, symbols}

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "captured_at": self.captured_at,
            "fingerprint_hash": self.fingerprint_hash,
            "total_libraries": self.total_libraries,
            "total_external_symbols": self.total_external_symbols,
            "libraries": self.libraries,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DependencyFingerprint":
        return cls(
            project_id=d["project_id"],
            captured_at=d["captured_at"],
            fingerprint_hash=d["fingerprint_hash"],
            total_libraries=d["total_libraries"],
            total_external_symbols=d["total_external_symbols"],
            libraries=d["libraries"],
        )

    def symbol_map(self) -> dict[str, str]:
        """id → signature for all symbols across all libraries."""
        result = {}
        for lib_data in self.libraries.values():
            for sym in lib_data["symbols"]:
                result[sym["id"]] = sym["signature"]
        return result

    def version_map(self) -> dict[str, str]:
        """library_name → version string (empty string if unknown)."""
        return {
            lib: data.get("version", "")
            for lib, data in self.libraries.items()
        }


# ── Fingerprinter ──────────────────────────────────────────────────────────────

class DependencyFingerprinter:
    def compute(
        self,
        project_id: str,
        deps: list[dict],
        project_path: str = "",
    ) -> DependencyFingerprint:
        """
        Compute a fingerprint from list_external_dependencies() output.

        If project_path is provided, reads the project lockfile to attach
        installed versions to each library. Version changes are included in the
        hash — a package upgrade changes the fingerprint even when the symbol
        surface looks identical.
        """
        lib_names = {lib["library"] for lib in deps}
        versions = resolve_versions(project_path, lib_names) if project_path else {}

        # Hash over (symbol_id, signature) pairs AND library versions so that a
        # version bump changes the fingerprint even with no symbol-level change.
        all_symbols = []
        for lib_entry in sorted(deps, key=lambda d: d["library"]):
            lib = lib_entry["library"]
            ver = versions.get(lib, "")
            for sym in sorted(lib_entry["symbols"], key=lambda s: s["id"]):
                all_symbols.append((sym["id"], sym["signature"], ver))

        fingerprint_hash = hashlib.sha256(
            json.dumps(all_symbols, sort_keys=True).encode()
        ).hexdigest()[:16]

        libraries = {
            lib["library"]: {
                "version": versions.get(lib["library"], "unknown"),
                "symbol_count": lib["symbol_count"],
                "symbols": lib["symbols"],
            }
            for lib in sorted(deps, key=lambda d: d["library"])
        }

        return DependencyFingerprint(
            project_id=project_id,
            captured_at=datetime.now(timezone.utc).isoformat(),
            fingerprint_hash=fingerprint_hash,
            total_libraries=len(deps),
            total_external_symbols=sum(lib["symbol_count"] for lib in deps),
            libraries=libraries,
        )

    def diff(
        self, old: DependencyFingerprint, new: DependencyFingerprint
    ) -> FingerprintDiff:
        """
        Compare two fingerprints.

        removed_symbols  — symbols gone since last index: most likely cause of
                           runtime ImportError / AttributeError.
        version_changes  — library version bumps with no symbol change: the
                           silent risk (internal behaviour changed, call site intact).
        """
        old_map = old.symbol_map()
        new_map = new.symbol_map()
        old_ids = set(old_map)
        new_ids = set(new_map)

        old_vers = old.version_map()
        new_vers = new.version_map()

        def lib_of(sym_id: str) -> str:
            return sym_id.replace("external.", "", 1).split(".")[0]

        version_changes = [
            {
                "library": lib,
                "old_version": old_vers[lib],
                "new_version": new_vers[lib],
            }
            for lib in sorted(set(old_vers) & set(new_vers))
            if old_vers[lib] != new_vers[lib]
            and old_vers[lib] != "unknown"
            and new_vers[lib] != "unknown"
        ]

        return FingerprintDiff(
            removed_symbols=[
                {"id": sid, "library": lib_of(sid), "signature": old_map[sid]}
                for sid in sorted(old_ids - new_ids)
            ],
            added_symbols=[
                {"id": sid, "library": lib_of(sid), "signature": new_map[sid]}
                for sid in sorted(new_ids - old_ids)
            ],
            changed_symbols=[
                {
                    "id": sid,
                    "library": lib_of(sid),
                    "old_signature": old_map[sid],
                    "new_signature": new_map[sid],
                }
                for sid in sorted(old_ids & new_ids)
                if old_map[sid] != new_map[sid]
            ],
            version_changes=version_changes,
        )
