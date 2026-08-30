"""Error types for the domain layer (Iteration A, WP-A1/A2/A3)."""
from __future__ import annotations


class DomainError(Exception):
    """Base error for the domain layer."""


class DomainPackValidationError(DomainError):
    """Raised when a Domain Pack is malformed.

    ``errors`` carries every explicit validation message so callers can show
    all problems at once instead of failing on the first one.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class ParseError(DomainError):
    """Raised when a markdown document does not match the domain template."""

    def __init__(self, message: str, *, line: int | None = None) -> None:
        self.line = line
        super().__init__(message)


class TranslationError(DomainError):
    """Base error for stage-1 translation."""


class NumberInvariantError(TranslationError):
    """Raised when the P2 number invariant is violated."""


class VerificationError(DomainError):
    """Base error for the verification engine."""


class AdjudicationError(DomainError):
    """Base error for the L3 adjudication queue."""


class AdjudicationNotFoundError(AdjudicationError):
    """Raised when an adjudication row does not exist."""


class AdjudicationAlreadyResolvedError(AdjudicationError):
    """Raised when deciding an adjudication that is not pending."""


class GlossaryProposalNotFoundError(AdjudicationError):
    """Raised when a glossary proposal row does not exist."""


class GlossaryProposalAlreadyResolvedError(AdjudicationError):
    """Raised when deciding a glossary proposal that is not pending."""
