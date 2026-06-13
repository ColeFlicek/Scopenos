"""
Reconcile phase — pure diff between old and new index state.

IndexDelta and reconcile() are the only things here. No I/O, no async,
no database dependencies. The Indexer calls reconcile() before any DB
writes; the result tells it exactly what to delete, what to re-embed,
and what to leave alone.

expected_vector_count is the invariant the coverage check enforces after
the Commit phase: every node in to_embed | unchanged must have a vector.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IndexDelta:
    """
    Pure description of what needs to change in the index.

    to_embed:   function IDs that need new vectors (new functions + bodies that changed)
    to_delete:  function IDs whose old vectors are now stale (changed + removed)
    unchanged:  function IDs whose body is identical — keep their existing vectors

    The sets are disjoint: a function ID appears in exactly one of
    {to_embed, unchanged} and at most one of {to_embed, to_delete}.
    """
    to_embed: frozenset[str]
    to_delete: frozenset[str]
    unchanged: frozenset[str]

    @property
    def expected_vector_count(self) -> int:
        """
        After a successful Commit, this many nodes should have vectors.
        Used by the coverage check to detect partial failures.
        """
        return len(self.to_embed) + len(self.unchanged)

    @property
    def functions_added(self) -> frozenset[str]:
        """IDs that are new (in to_embed but not in to_delete)."""
        return self.to_embed - self.to_delete

    @property
    def functions_changed(self) -> frozenset[str]:
        """IDs whose body changed (in both to_embed and to_delete)."""
        return self.to_embed & self.to_delete

    @property
    def functions_removed(self) -> frozenset[str]:
        """IDs that were deleted (in to_delete but not in to_embed)."""
        return self.to_delete - self.to_embed


def reconcile(
    old_hashes: dict[str, str],
    new_hashes: dict[str, str],
) -> IndexDelta:
    """
    Diff old DB state against freshly parsed state.

    old_hashes: {function_id: body_hash} — what is currently in the DB
    new_hashes: {function_id: body_hash} — what the parser just produced

    Returns an IndexDelta describing exactly what the Commit phase must do.
    Pure function: no I/O, deterministic, testable with plain dicts.
    """
    existing = set(old_hashes)
    fresh = set(new_hashes)

    truly_new = fresh - existing
    truly_deleted = existing - fresh
    shared = existing & fresh

    changed = {nid for nid in shared if new_hashes[nid] != old_hashes[nid]}
    unchanged = shared - changed

    return IndexDelta(
        to_embed=frozenset(changed | truly_new),
        to_delete=frozenset(changed | truly_deleted),
        unchanged=frozenset(unchanged),
    )
