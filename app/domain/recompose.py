"""WP-A6 recomposer: Neo4j domain graph -> canonical.md.

``recompose_document`` is the exact inverse of
:func:`app.domain.extract.extract_document`. It reads the :Document node, the
ingredient Entities (with their ``qty``/``unit`` Facts) and the step Entities,
then renders the Appendix A canonical markdown with the same frontmatter order,
section separators, line format and single trailing newline used by
:func:`app.domain.canonical.canonicalize`.
"""
from __future__ import annotations

from typing import Any

from app.domain.canonical import render_canonical_md
from app.domain.verify import IngredientLine
from app.rag.cache import recompose_cache
from app.storage.client import Neo4jClient
from app.storage.errors import NotFoundError


def recompose_document(client: Neo4jClient, doc_id: str) -> str:
    """Reconstruct the canonical.md for ``doc_id`` from the domain graph.

    Raises :class:`app.storage.errors.NotFoundError` when the :Document does
    not exist.

    WP-B5: the rendered markdown is cached in-process with a TTL
    (``KM_RAG_CACHE_TTL``, default 300s) keyed by ``(neo4j uri, doc_id)`` and
    invalidated on ingest (``extract_document``). Documents are immutable
    once extracted, so the cache is safe; the TTL bounds staleness for any
    other graph mutation (e.g. fact invalidation).
    """
    key = (client.config.uri, doc_id)
    cached = recompose_cache.get(key)
    if cached is not None:
        return cached
    with client.session() as session:
        document_record = session.run(
            "MATCH (d:Document {id: $doc_id}) RETURN d",
            doc_id=doc_id,
        ).single()
        if document_record is None:
            raise NotFoundError(f"Document {doc_id!r} not found")

        document = dict(document_record["d"])

        ingredient_records = session.run(
            """
            MATCH (e:Entity {type: 'ingredient'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            RETURN e.position AS position,
                   e.label AS item,
                   e.code AS code,
                   e.waste AS waste,
                   e.component AS component,
                   e.qty_max AS qty_max,
                   e.to_taste AS to_taste,
                   e.state AS state,
                   e.prep AS prep,
                   [(e)-[:HAS_FACT]->(f:Fact)
                    WHERE f.valid_to IS NULL AND f.property = 'qty' | f.value][0] AS qty,
                   [(e)-[:HAS_FACT]->(f:Fact)
                    WHERE f.valid_to IS NULL AND f.property = 'unit' | f.value][0] AS unit
            ORDER BY e.position
            """,
            doc_id=doc_id,
        )
        ingredients = [
            IngredientLine(
                raw="",
                qty=record["qty"],
                unit=record["unit"],
                item=record["item"],
                code=record["code"],
                waste=record["waste"],
                component=record["component"],
                qty_max=record["qty_max"],
                to_taste=bool(record["to_taste"]),
                state=tuple(record["state"] or ()),
                prep=record["prep"],
            )
            for record in ingredient_records
        ]

        step_records = session.run(
            """
            MATCH (e:Entity {type: 'step'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            RETURN e.position AS position, e.label AS text
            ORDER BY e.position
            """,
            doc_id=doc_id,
        )
        steps = [record["text"] for record in step_records]

    frontmatter: dict[str, Any] = {
        "title": document.get("title", ""),
        "id": document.get("document_id", document.get("id", "")),
        "lang": document.get("lang", "en"),
        "source_lang": document.get("source_lang", ""),
        "servings": document.get("servings", ""),
        "verification_level": document.get("verification_level", "L1"),
        "canonical_version": document.get("canonical_version", 1),
    }
    # time_min/difficulty: solo se presenti (card MSC EN-native non li hanno)
    if document.get("time_min") is not None:
        frontmatter["time_min"] = document["time_min"]
    if document.get("difficulty") is not None:
        frontmatter["difficulty"] = document["difficulty"]
    rendered = render_canonical_md(frontmatter, ingredients, steps)
    recompose_cache.set(key, rendered)
    return rendered
