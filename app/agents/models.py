"""Pydantic schemas for the Iteration C agent pipeline (WP-C1..C4).

Two contracts are shared by every agent:

- :class:`DomainBrief` — the versioned, structured output of the Domain
  Analyst (WP-C1). It carries candidate entities with frequencies, the
  vocabularies to normalize, detected units, ambiguities and candidate
  external ontologies (P7).
- :class:`AgentReport` — the uniform report envelope returned by the Codegen
  (WP-C3) and Evaluator (WP-C4) agents so the pipeline can gate on metrics
  without coupling to a specific agent implementation.

Both models are strict by default: unknown fields are rejected and every
required field is validated, so a malformed agent output fails explicitly
instead of being silently accepted downstream.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

BRIEF_SCHEMA_VERSION = "1.0"

VALID_KINDS = {"ingredient", "technique", "state"}


def utc_now_iso() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(UTC).isoformat()


class CandidateEntity(BaseModel):
    """One candidate domain entity observed in the corpus.

    ``term`` is the canonical English label (or the source term when no
    translation is available); ``source_terms`` are the Italian surface forms
    that must normalize to it.
    """

    model_config = ConfigDict(extra="forbid")

    term: str
    source_terms: list[str] = Field(default_factory=list)
    frequency: int = Field(default=1, ge=1)
    kind: str = "ingredient"
    contexts: list[str] = Field(default_factory=list)
    ontology_uri: str | None = None

    @field_validator("term")
    @classmethod
    def _term_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("term must not be empty")
        return value

    @field_validator("kind")
    @classmethod
    def _kind_valid(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in VALID_KINDS:
            raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}, got {value!r}")
        return value


class Vocabulary(BaseModel):
    """A named vocabulary to normalize (tecnica, ingredienti, stati)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    entries: list[CandidateEntity] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("name must not be empty")
        return value


class UnitObservation(BaseModel):
    """A unit token observed in the corpus with its frequency."""

    model_config = ConfigDict(extra="forbid")

    unit: str
    frequency: int = Field(default=1, ge=1)
    examples: list[str] = Field(default_factory=list)

    @field_validator("unit")
    @classmethod
    def _unit_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("unit must not be empty")
        return value


class Ambiguity(BaseModel):
    """A normalization ambiguity that must reach the human gate (P5)."""

    model_config = ConfigDict(extra="forbid")

    term: str
    candidates: list[str] = Field(default_factory=list)
    note: str = ""

    @field_validator("term")
    @classmethod
    def _term_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("term must not be empty")
        return value


class OntologyCandidate(BaseModel):
    """An external ontology candidate (P7: standards before proprietary)."""

    model_config = ConfigDict(extra="forbid")

    prefix: str
    uri: str
    note: str = ""

    @field_validator("prefix", "uri")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class DomainBrief(BaseModel):
    """Versioned domain brief produced by the Domain Analyst (WP-C1)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = BRIEF_SCHEMA_VERSION
    domain: str
    language: str
    canonical_language: str
    version: str
    generated_at: str = Field(default_factory=utc_now_iso)
    corpus_size: int = Field(default=0, ge=0)
    entities: list[CandidateEntity] = Field(default_factory=list)
    vocabularies: list[Vocabulary] = Field(default_factory=list)
    units: list[UnitObservation] = Field(default_factory=list)
    ambiguities: list[Ambiguity] = Field(default_factory=list)
    ontologies: list[OntologyCandidate] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)

    @field_validator("domain", "language", "canonical_language", "version")
    @classmethod
    def _non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    def entity_map(self) -> dict[str, CandidateEntity]:
        """Return ``term -> entity`` for the flat entity list."""
        return {entity.term: entity for entity in self.entities}

    def vocabulary(self, name: str) -> Vocabulary | None:
        """Return the vocabulary named ``name``, or ``None``."""
        for vocabulary in self.vocabularies:
            if vocabulary.name == name:
                return vocabulary
        return None


class AgentReport(BaseModel):
    """Uniform report envelope for Codegen and Evaluator agents."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    status: str = "ok"
    summary: str = ""
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    timestamp: str = Field(default_factory=utc_now_iso)

    @field_validator("agent")
    @classmethod
    def _agent_non_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("agent must not be empty")
        return value

    @field_validator("status")
    @classmethod
    def _status_valid(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"ok", "failed"}:
            raise ValueError(f"status must be 'ok' or 'failed', got {value!r}")
        return value
