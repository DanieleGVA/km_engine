"""Conflict detection and resolution workflow (WP6, Gate G6).

Public API:
- :func:`scan_conflicts` — full or entity-scoped scan of the Neo4j graph.
- :func:`detect_conflicts_for_entity` — targeted scan for one Entity.
- :func:`post_ingest_hook` — hook to run after ingestion for a set of Entities.
- :func:`list_conflicts` / :func:`get_conflict` — read the Postgres workflow.
- :func:`approve_conflict` / :func:`reject_conflict` — resolution workflow.

Detection rule (FR6.1): two current Facts (``valid_to IS NULL``,
``status = 'valid'``) on the same Entity and property, with different values
and different ``source_id`` values, are a conflict.

Suggestion rule (Q10): higher confidence wins first
(EXTRACTED > INFERRED > AMBIGUOUS); on equal confidence the fact whose Source
has the most recent ``ingested_at`` wins; on a further tie choice ``b`` wins.
"""
from __future__ import annotations

from .detection import (
    detect_conflicts_for_entity,
    post_ingest_hook,
    scan_conflicts,
)
from .errors import (
    ConflictAlreadyResolvedError,
    ConflictError,
    ConflictNotFoundError,
    ConflictResolutionError,
    InvalidChoiceError,
)
from .workflow import (
    approve_conflict,
    get_conflict,
    list_conflicts,
    reject_conflict,
)

__all__ = [
    "ConflictAlreadyResolvedError",
    "ConflictError",
    "ConflictNotFoundError",
    "ConflictResolutionError",
    "InvalidChoiceError",
    "approve_conflict",
    "detect_conflicts_for_entity",
    "get_conflict",
    "list_conflicts",
    "post_ingest_hook",
    "reject_conflict",
    "scan_conflicts",
]
