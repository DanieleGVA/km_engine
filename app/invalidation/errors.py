"""Errors for the truth-maintenance module (WP6, Gate G7)."""
from __future__ import annotations


class InvalidationError(Exception):
    """Base error for source invalidation."""


class SourceNotFoundError(InvalidationError):
    """Raised when the Source node to invalidate does not exist."""
