"""Dataclasses shared by the WP4 ingestion pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IngestJob:
    """A row from the ``ingest_jobs`` Postgres table."""

    id: int
    source_uri: str
    type: str
    status: str
    progress: int
    error: str | None
    created_by: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class ExtractionResult:
    """Nodes and edges produced by the code extractor (graphify shape)."""

    nodes: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class EntityRecord:
    """An Entity to write to Neo4j."""

    entity_id: str
    label: str
    type: str | None
    source_file: str | None
    source_location: str | None
    confidence: str
    language: str = "en"
    translation_state: str = "native"
    source_language: str | None = None


@dataclass(frozen=True)
class FactRecord:
    """A Fact to write to Neo4j and link to its Entity."""

    fact_id: str
    entity_id: str
    property: str
    value: str
    source_id: str | None
    confidence: str
    language: str = "en"
    translation_state: str = "native"
    source_language: str | None = None


@dataclass(frozen=True)
class RelationRecord:
    """A RELATES_TO arc to write to Neo4j."""

    source_entity_id: str
    target_entity_id: str
    relation: str
    confidence: str
    source_id: str | None
    source_file: str | None = None
    source_location: str | None = None


@dataclass(frozen=True)
class CandidateFact:
    """A candidate fact returned by a SemanticService.

    ``entity_label`` groups facts that belong to the same extracted entity.
    ``language``/``translation_state`` describe the canonical representation
    (FR9): the stub returns English facts with ``translation_state='pending'``
    for non-English sources.
    """

    entity_label: str
    property: str
    value: str
    confidence: str = "INFERRED"
    language: str = "en"
    translation_state: str = "native"
    source_language: str | None = None


@dataclass(frozen=True)
class FileInfo:
    """Per-file bookkeeping for a chunk."""

    path: Path
    rel: str
    content_hash: str
    source_id: str
