"""Errors for the conflict detection and resolution workflow (WP6)."""
from __future__ import annotations


class ConflictError(Exception):
    """Base error for conflict operations."""


class ConflictNotFoundError(ConflictError):
    """Raised when a conflict row does not exist."""


class ConflictAlreadyResolvedError(ConflictError):
    """Raised when trying to resolve a conflict that is not pending."""


class InvalidChoiceError(ConflictError):
    """Raised when the approve choice is not ``a`` or ``b``."""


class ConflictResolutionError(ConflictError):
    """Raised when the chosen/losing fact cannot be applied in the graph."""
