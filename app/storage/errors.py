"""Storage layer exceptions."""

from __future__ import annotations


class StorageError(Exception):
    """Base error for the Neo4j storage layer."""


class ConnectionError(StorageError):
    """Raised when the Neo4j connection cannot be established."""


class NotFoundError(StorageError):
    """Raised when a requested node or relationship does not exist."""


class AlreadyExistsError(StorageError):
    """Raised when creating a node with an id that already exists."""


class ValidationError(StorageError):
    """Raised when an input value is outside the allowed domain."""
