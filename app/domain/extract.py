"""WP-A6 extractor: canonical.md -> Neo4j domain graph.

``extract_document`` is the inverse of :func:`app.domain.recompose.recompose_document`.
It parses the Appendix A canonical markdown, MERGEs a :Document node keyed by
``doc_id``, registers the canonical.md as a :Source, and writes the domain
sub-graph:

- one :Entity per ingredient (type ``ingredient``) with ``qty``/``unit`` Facts;
- one :Entity per method step (type ``step``) so the exact step text and order
  survive for the round-trip;
- one :Entity per technique/state term found in the steps (type
  ``technique``/``state``) with ``NORMALIZED_TO`` the bootstrap :CanonicalTerm;
- time/temperature Facts attached to the step Entities.

Every write is a MERGE on a deterministic id derived from ``doc_id`` and the
position/term, so re-extracting the same ``doc_id`` + ``canonical_hash`` is
idempotent (T7-bis): no duplicate nodes or relationships.

The frontmatter ``id`` is preserved separately as ``Document.document_id``
because the graph key ``Document.id`` may be namespaced by the caller (tests
use the ``ia6_`` prefix) while the round-trip must reproduce the original
frontmatter id byte-for-byte.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import psycopg

from app.domain.pack import DomainPackBundle
from app.domain.verify import parse_translated_md
from app.storage.client import Neo4jClient

# Content numbers followed by a time unit (Italian corpus + canonical EN).
_TIME_RE = re.compile(
    r"-?\d+(?:\.\d+)?\s*(?:minuti|min\b|ore|h\b|secondi|sec\b|s\b)",
    flags=re.IGNORECASE,
)
# Content numbers followed by a temperature unit.
_TEMPERATURE_RE = re.compile(
    r"-?\d+(?:\.\d+)?\s*(?:°C|°c|gradi|degrees)\b",
    flags=re.IGNORECASE,
)

TRANSLATION_STATE = "translated"


@dataclass
class DocumentRecord:
    """Counts produced by one :func:`extract_document` run."""

    document_id: str
    canonical_hash: str
    entities: int
    facts: int
    sources: int
    terms_linked: int


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_hash(canonical_md: str) -> str:
    return hashlib.sha256(canonical_md.encode("utf-8")).hexdigest()


def _source_id(doc_id: str) -> str:
    return f"{doc_id}:source"


def _source_uri(doc_id: str) -> str:
    return f"canonical://{doc_id}.md"


def _pack_id(pack: DomainPackBundle) -> str:
    return f"{pack.pack.name}:{pack.pack.version}"


def _glossary_index(
    pack: DomainPackBundle,
) -> dict[str, tuple[str, Any]]:
    """Map ``labels_en.casefold() -> (namespace, entry)`` for the glossaries."""
    by_label_en: dict[str, tuple[str, Any]] = {}
    for namespace in ("tecnica", "ingredienti", "stati"):
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            key = entry.labels_en.strip().casefold()
            if key:
                by_label_en.setdefault(key, (namespace, entry))
    return by_label_en


def _term_id(namespace: str, entry_id: str) -> str:
    return f"{namespace}:{entry_id}"


def _find_step_terms(
    step: str, pack: DomainPackBundle
) -> list[tuple[str, Any]]:
    """Return ``(namespace, entry)`` for technique/state terms in ``step``.

    Matching is deterministic and case-insensitive on the canonical
    ``labels_en`` with word boundaries. Only the technique and state
    glossaries are considered: ingredients are extracted from the
    Ingredients section, not from the Method text.
    """
    found: list[tuple[str, Any]] = []
    text = step.casefold()
    for namespace in ("tecnica", "stati"):
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            label = entry.labels_en.strip()
            if not label:
                continue
            pattern = re.compile(rf"\b{re.escape(label.casefold())}\b")
            if pattern.search(text):
                found.append((namespace, entry))
    return found


def _extract_step_facts(step: str) -> list[tuple[str, str]]:
    """Return ``(property, value)`` time/temperature facts for one step."""
    facts: list[tuple[str, str]] = []
    for match in _TIME_RE.finditer(step):
        facts.append(("time", match.group(0)))
    for match in _TEMPERATURE_RE.finditer(step):
        facts.append(("temperature", match.group(0)))
    return facts


def extract_document(
    client: Neo4jClient,
    conn: psycopg.Connection | None,
    doc_id: str,
    canonical_md: str,
    pack: DomainPackBundle,
) -> DocumentRecord:
    """Extract a canonical.md into the Neo4j domain graph (idempotent).

    ``conn`` is accepted for pipeline symmetry with the other domain stages and
    is currently unused: the extractor writes only to Neo4j (canon-log and the
    proposal/adjudication queues belong to WP-A5/A3).
    """
    del conn  # reserved for future domain_jobs persistence (WP-A7+)

    parsed = parse_translated_md(canonical_md, known_units=pack.known_units())
    frontmatter = parsed.frontmatter
    digest = _canonical_hash(canonical_md)
    now = _now()
    source_id = _source_id(doc_id)
    source_uri = _source_uri(doc_id)
    pack_id = _pack_id(pack)
    by_label_en = _glossary_index(pack)

    document_id = str(frontmatter.get("id", doc_id))
    title = str(frontmatter.get("title", ""))
    source_lang = str(frontmatter.get("source_lang", pack.language))
    servings = int(frontmatter["servings"])
    time_min = int(frontmatter["time_min"])
    difficulty = str(frontmatter["difficulty"])
    verification_level = str(frontmatter.get("verification_level", "L1"))
    canonical_version = int(frontmatter.get("canonical_version", 1))

    ingredient_rows: list[dict[str, Any]] = []
    for index, ingredient in enumerate(parsed.ingredients):
        resolved = by_label_en.get(ingredient.item.strip().casefold())
        ingredient_rows.append(
            {
                "entity_id": f"{doc_id}:ing:{index}",
                "label": ingredient.item,
                "position": index,
                "qty": ingredient.qty,
                "unit": ingredient.unit,
                "term_id": _term_id(resolved[0], resolved[1].id)
                if resolved is not None
                else None,
            }
        )

    step_rows: list[dict[str, Any]] = []
    term_rows: list[dict[str, Any]] = []
    step_fact_rows: list[dict[str, Any]] = []
    for index, step in enumerate(parsed.steps):
        step_entity_id = f"{doc_id}:step:{index}"
        step_rows.append(
            {
                "entity_id": step_entity_id,
                "label": step,
                "position": index,
            }
        )
        for namespace, entry in _find_step_terms(step, pack):
            term_rows.append(
                {
                    "entity_id": f"{doc_id}:{namespace}:{entry.id}:{index}",
                    "label": entry.labels_en,
                    "type": "technique" if namespace == "tecnica" else "state",
                    "position": index,
                    "term_id": _term_id(namespace, entry.id),
                }
            )
        for occurrence, (property_name, value) in enumerate(
            _extract_step_facts(step)
        ):
            step_fact_rows.append(
                {
                    "fact_id": f"{step_entity_id}:{property_name}:{occurrence}",
                    "entity_id": step_entity_id,
                    "property": property_name,
                    "value": value,
                }
            )

    entities_count = len(ingredient_rows) + len(step_rows) + len(term_rows)
    facts_count = (
        len(ingredient_rows)  # one qty fact per ingredient
        + sum(1 for row in ingredient_rows if row["unit"] is not None)
        + len(step_fact_rows)
    )
    terms_linked = sum(1 for row in ingredient_rows if row["term_id"]) + len(
        term_rows
    )

    def work(tx: Any) -> None:
        tx.run(
            """
            MERGE (d:Document {id: $doc_id})
            SET d.document_id = $document_id,
                d.title = $title,
                d.lang = 'en',
                d.source_lang = $source_lang,
                d.canonical_hash = $hash,
                d.verification_level = $verification_level,
                d.translation_state = $translation_state,
                d.source_language = $source_language,
                d.servings = $servings,
                d.time_min = $time_min,
                d.difficulty = $difficulty,
                d.canonical_version = $canonical_version,
                d.is_public = false,
                d.roles = [],
                d.teams = []
            """,
            doc_id=doc_id,
            document_id=document_id,
            title=title,
            source_lang=source_lang,
            hash=digest,
            verification_level=verification_level,
            translation_state=TRANSLATION_STATE,
            source_language=source_lang,
            servings=servings,
            time_min=time_min,
            difficulty=difficulty,
            canonical_version=canonical_version,
        )

        tx.run(
            """
            MERGE (s:Source {id: $source_id})
            SET s.uri = $uri,
                s.type = 'file',
                s.hash = $hash,
                s.language = 'en',
                s.ingested_at = $now
            """,
            source_id=source_id,
            uri=source_uri,
            hash=digest,
            now=now,
        )

        tx.run(
            """
            MATCH (d:Document {id: $doc_id})
            MATCH (p:DomainPack {id: $pack_id})
            MERGE (d)-[:PART_OF_PACK]->(p)
            """,
            doc_id=doc_id,
            pack_id=pack_id,
        )

        for row in ingredient_rows:
            tx.run(
                """
                MERGE (e:Entity {id: $entity_id})
                SET e.label = $label,
                    e.type = 'ingredient',
                    e.position = $position,
                    e.source_file = $source_uri,
                    e.confidence = 'EXTRACTED',
                    e.is_public = false,
                    e.roles = [],
                    e.teams = []
                WITH e
                MATCH (d:Document {id: $doc_id})
                MERGE (e)-[:PART_OF_DOC]->(d)
                """,
                entity_id=row["entity_id"],
                label=row["label"],
                position=row["position"],
                source_uri=source_uri,
                doc_id=doc_id,
            )
            if row["term_id"] is not None:
                tx.run(
                    """
                    MATCH (e:Entity {id: $entity_id})
                    MATCH (t:CanonicalTerm {id: $term_id})
                    MERGE (e)-[:NORMALIZED_TO]->(t)
                    """,
                    entity_id=row["entity_id"],
                    term_id=row["term_id"],
                )
            tx.run(
                """
                MATCH (e:Entity {id: $entity_id})
                MATCH (s:Source {id: $source_id})
                MERGE (f:Fact {id: $fact_id})
                SET f.logical_id = $fact_id,
                    f.property = 'qty',
                    f.value = $value,
                    f.valid_from = $now,
                    f.status = 'valid',
                    f.confidence = 'EXTRACTED',
                    f.source_id = $source_id
                MERGE (e)-[:HAS_FACT]->(f)
                MERGE (f)-[:DERIVED_FROM]->(s)
                """,
                entity_id=row["entity_id"],
                source_id=source_id,
                fact_id=f"{row['entity_id']}:qty",
                value=row["qty"],
                now=now,
            )
            if row["unit"] is not None:
                tx.run(
                    """
                    MATCH (e:Entity {id: $entity_id})
                    MATCH (s:Source {id: $source_id})
                    MERGE (f:Fact {id: $fact_id})
                    SET f.logical_id = $fact_id,
                        f.property = 'unit',
                        f.value = $value,
                        f.valid_from = $now,
                        f.status = 'valid',
                        f.confidence = 'EXTRACTED',
                        f.source_id = $source_id
                    MERGE (e)-[:HAS_FACT]->(f)
                    MERGE (f)-[:DERIVED_FROM]->(s)
                    """,
                    entity_id=row["entity_id"],
                    source_id=source_id,
                    fact_id=f"{row['entity_id']}:unit",
                    value=row["unit"],
                    now=now,
                )

        for row in step_rows:
            tx.run(
                """
                MERGE (e:Entity {id: $entity_id})
                SET e.label = $label,
                    e.type = 'step',
                    e.position = $position,
                    e.source_file = $source_uri,
                    e.confidence = 'EXTRACTED',
                    e.is_public = false,
                    e.roles = [],
                    e.teams = []
                WITH e
                MATCH (d:Document {id: $doc_id})
                MERGE (e)-[:PART_OF_DOC]->(d)
                """,
                entity_id=row["entity_id"],
                label=row["label"],
                position=row["position"],
                source_uri=source_uri,
                doc_id=doc_id,
            )

        for row in term_rows:
            tx.run(
                """
                MERGE (e:Entity {id: $entity_id})
                SET e.label = $label,
                    e.type = $type,
                    e.position = $position,
                    e.source_file = $source_uri,
                    e.confidence = 'EXTRACTED',
                    e.is_public = false,
                    e.roles = [],
                    e.teams = []
                WITH e
                MATCH (d:Document {id: $doc_id})
                MERGE (e)-[:PART_OF_DOC]->(d)
                """,
                entity_id=row["entity_id"],
                label=row["label"],
                type=row["type"],
                position=row["position"],
                source_uri=source_uri,
                doc_id=doc_id,
            )
            tx.run(
                """
                MATCH (e:Entity {id: $entity_id})
                MATCH (t:CanonicalTerm {id: $term_id})
                MERGE (e)-[:NORMALIZED_TO]->(t)
                """,
                entity_id=row["entity_id"],
                term_id=row["term_id"],
            )

        for row in step_fact_rows:
            tx.run(
                """
                MATCH (e:Entity {id: $entity_id})
                MATCH (s:Source {id: $source_id})
                MERGE (f:Fact {id: $fact_id})
                SET f.logical_id = $fact_id,
                    f.property = $property,
                    f.value = $value,
                    f.valid_from = $now,
                    f.status = 'valid',
                    f.confidence = 'EXTRACTED',
                    f.source_id = $source_id
                MERGE (e)-[:HAS_FACT]->(f)
                MERGE (f)-[:DERIVED_FROM]->(s)
                """,
                entity_id=row["entity_id"],
                source_id=source_id,
                fact_id=row["fact_id"],
                property=row["property"],
                value=row["value"],
                now=now,
            )

    with client.session() as session:
        session.execute_write(work)

    return DocumentRecord(
        document_id=doc_id,
        canonical_hash=digest,
        entities=entities_count,
        facts=facts_count,
        sources=1,
        terms_linked=terms_linked,
    )
