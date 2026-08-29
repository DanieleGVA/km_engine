"""Truth-maintenance: source invalidation and propagation (WP6, Gate G7).

Public API:
- :func:`invalidate_source` — invalidate Facts derived from a Source and
  propagate to dependent Facts.

Dependency rule (documented): a current Fact ``D`` depends on a parent Fact
``P`` when all of the following hold:
1. ``D.valid_to IS NULL`` (current version);
2. ``D.confidence = 'INFERRED'``;
3. ``D`` is linked to ``P`` by an explicit fact-to-fact ``DERIVED_FROM``
   edge, or (fallback) ``D`` and ``P`` belong to the same Entity (same
   ``HAS_FACT`` parent);
4. ``D`` is not itself directly invalidated by the source.

Propagation is recursive with a documented depth limit (default 3, max 10).
Dependent Facts are marked ``under_review`` (not ``obsolete``): they are not
deleted, they wait for recomputation/verification. Entity-level propagation
through ``RELATES_TO`` is left as an open point for WP7/WP8.
"""
from __future__ import annotations

from .errors import InvalidationError, SourceNotFoundError
from .maintenance import DEFAULT_MAX_DEPTH, MAX_DEPTH_LIMIT, invalidate_source

__all__ = [
    "DEFAULT_MAX_DEPTH",
    "MAX_DEPTH_LIMIT",
    "InvalidationError",
    "SourceNotFoundError",
    "invalidate_source",
]
