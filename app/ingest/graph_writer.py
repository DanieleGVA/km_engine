"""Write ingestion records to Neo4j.

Entity/Fact/Relation writes go through :class:`app.storage.repository.GraphRepository`
(ADR-001 D1). Source nodes, DERIVED_FROM links and the FR9 language metadata
properties are written with direct Cypher because the baseline repository does
not expose those fields (and WP4 must not modify ``app/storage/repository.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from neo4j import ManagedTransaction

from app.ingest.models import EntityRecord, FactRecord, RelationRecord
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository


def _now() -> datetime:
    return datetime.now(UTC)


class GraphWriter:
    """Idempotent writer for the ingestion pipeline."""

    def __init__(self, repo: GraphRepository, client: Neo4jClient) -> None:
        self.repo = repo
        self.client = client

    def _write(self, fn: Any) -> Any:
        with self.client.session() as session:
            return session.execute_write(fn)

    # ------------------------------------------------------------------ Source
    def upsert_source(
        self,
        *,
        source_id: str,
        uri: str,
        content_hash: str,
        type: str,
        language: str,
    ) -> None:
        """Create or refresh a :Source node with the real content hash."""

        def work(tx: ManagedTransaction) -> None:
            tx.run(
                """
                MERGE (s:Source {id: $id})
                SET s.uri = $uri,
                    s.type = $type,
                    s.hash = $hash,
                    s.language = $language,
                    s.ingested_at = $ingested_at
                """,
                id=source_id,
                uri=uri,
                type=type,
                hash=content_hash,
                language=language,
                ingested_at=_now(),
            )

        self._write(work)

    # ------------------------------------------------------------------ Entity
    def upsert_entity(self, record: EntityRecord) -> None:
        """Create or update an Entity, then set FR9 language metadata."""
        existing = self.repo.get_entity(record.entity_id)
        if existing is None:
            self.repo.create_entity(
                entity_id=record.entity_id,
                label=record.label,
                type=record.type,
                source_file=record.source_file,
                source_location=record.source_location,
                confidence=record.confidence,
            )
        else:
            self.repo.update_entity(
                record.entity_id,
                label=record.label,
                type=record.type,
                source_file=record.source_file,
                source_location=record.source_location,
                confidence=record.confidence,
            )
        self._set_entity_language(record)

    def _set_entity_language(self, record: EntityRecord) -> None:
        def work(tx: ManagedTransaction) -> None:
            tx.run(
                """
                MATCH (e:Entity {id: $id})
                SET e.language = $language,
                    e.translation_state = $translation_state,
                    e.source_language = $source_language
                """,
                id=record.entity_id,
                language=record.language,
                translation_state=record.translation_state,
                source_language=record.source_language,
            )

        self._write(work)

    # -------------------------------------------------------------------- Fact
    def upsert_fact(self, record: FactRecord) -> None:
        """Create or version a Fact, then set language metadata and provenance."""
        existing = self.repo.get_fact(record.fact_id)
        if existing is None:
            self.repo.create_fact(
                fact_id=record.fact_id,
                entity_id=record.entity_id,
                property=record.property,
                value=record.value,
                source_id=record.source_id,
                confidence=record.confidence,
            )
        else:
            if (
                existing.get("value") != record.value
                or existing.get("property") != record.property
            ):
                self.repo.update_fact(
                    record.fact_id,
                    value=record.value,
                    property=record.property,
                    confidence=record.confidence,
                    source_id=record.source_id,
                )
        self._set_fact_language(record)
        if record.source_id:
            self._link_fact_to_source(record.fact_id, record.source_id)

    def _set_fact_language(self, record: FactRecord) -> None:
        def work(tx: ManagedTransaction) -> None:
            tx.run(
                """
                MATCH (f:Fact {id: $id})
                SET f.language = $language,
                    f.translation_state = $translation_state,
                    f.source_language = $source_language
                """,
                id=record.fact_id,
                language=record.language,
                translation_state=record.translation_state,
                source_language=record.source_language,
            )

        self._write(work)

    def _link_fact_to_source(self, fact_id: str, source_id: str) -> None:
        def work(tx: ManagedTransaction) -> None:
            tx.run(
                """
                MATCH (f:Fact {id: $fact_id})
                MATCH (s:Source {id: $source_id})
                MERGE (f)-[:DERIVED_FROM]->(s)
                """,
                fact_id=fact_id,
                source_id=source_id,
            )

        self._write(work)

    # ------------------------------------------------------------- RELATES_TO
    def upsert_relation(self, record: RelationRecord) -> None:
        """Create/refresh a RELATES_TO arc and set file provenance."""
        self.repo.create_relation(
            source_entity_id=record.source_entity_id,
            target_entity_id=record.target_entity_id,
            relation=record.relation,
            confidence=record.confidence,
            source_id=record.source_id,
        )
        self._set_relation_provenance(record)

    def _set_relation_provenance(self, record: RelationRecord) -> None:
        def work(tx: ManagedTransaction) -> None:
            tx.run(
                """
                MATCH (a:Entity {id: $source})
                MATCH (b:Entity {id: $target})
                MATCH (a)-[r:RELATES_TO {relation: $relation}]->(b)
                SET r.source_file = $source_file,
                    r.source_location = $source_location
                """,
                source=record.source_entity_id,
                target=record.target_entity_id,
                relation=record.relation,
                source_file=record.source_file,
                source_location=record.source_location,
            )

        self._write(work)
