"""Error types for the WP4 ingestion pipeline."""

from __future__ import annotations


class IngestError(Exception):
    """Base error for the ingestion layer."""


class JobNotFoundError(IngestError):
    """Raised when an ingest job does not exist."""


class InvalidJobStateError(IngestError):
    """Raised when a job cannot be resumed or its state is inconsistent."""


class UnsupportedFileTypeError(IngestError):
    """Raised when a file type is not supported by the configured job type."""
