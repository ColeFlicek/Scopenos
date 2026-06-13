"""
Coverage check — post-commit audit of the whole project index.

IndexCoverage describes the current state of a project's index after an
index operation completes. It answers three questions:

  1. Are all indexed functions represented by a vector?       (missing_vectors)
  2. Were any of those vectors computed from empty content?   (degraded_count)
  3. How many functions could be upgraded with enrich_summaries? (on_large_model)

The status and recommendation properties turn raw counts into actionable output
that the Indexer includes in its result dict on every run.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class IndexCoverage:
    """
    Post-commit audit of a project's index health.

    expected:       total nodes currently in the call graph
    actual:         nodes that have an embedding vector
    missing_vectors: IDs of nodes with no vector at all
    degraded_count: nodes that have a vector but were embedded from empty content
    on_large_model: nodes using text-embedding-3-large fallback (quality can be improved)
    """
    project_id: str
    expected: int
    actual: int
    missing_vectors: list[str]
    degraded_count: int
    on_large_model: int

    @property
    def gap(self) -> int:
        """Number of nodes that should have vectors but don't."""
        return len(self.missing_vectors)

    @property
    def status(self) -> str:
        """
        "ok"       — every node has a meaningful vector
        "partial"  — some nodes have no vector at all (data integrity issue)
        "degraded" — all nodes have vectors but some were embedded from empty content
        """
        if self.gap > 0:
            return "partial"
        if self.degraded_count > 0:
            return "degraded"
        return "ok"

    @property
    def recommendation(self) -> str | None:
        """Plain-English fix, prioritising data integrity over quality."""
        if self.gap > 0:
            return (
                f"reembed_project('{self.project_id}') — "
                f"{self.gap} function(s) have no embedding vector"
            )
        if self.degraded_count > 0:
            return (
                f"enrich_summaries('{self.project_id}') — "
                f"{self.degraded_count} function(s) were embedded from empty content"
            )
        if self.on_large_model > 0:
            return (
                f"enrich_summaries('{self.project_id}') — "
                f"{self.on_large_model} function(s) can be upgraded from large-model fallback"
            )
        return None

    def as_dict(self) -> dict:
        """Serialisable summary for inclusion in index result dicts."""
        d: dict = {
            "status": self.status,
            "expected": self.expected,
            "actual": self.actual,
            "gap": self.gap,
            "degraded": self.degraded_count,
            "on_large_model": self.on_large_model,
        }
        if self.missing_vectors:
            d["missing_vector_ids"] = self.missing_vectors
        if self.recommendation:
            d["recommendation"] = self.recommendation
        return d
