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

from app.domain.canonical import CANONICAL_FRONTMATTER_ORDER
from app.storage.client import Neo4jClient
from app.storage.errors import NotFoundError


def _render_canonical_md(
    frontmatter: dict[str, Any],
    ingredients: list[tuple[str, str | None, str]],
    steps: list[str],
) -> str:
    """Render Appendix A markdown (mirrors ``canonical._render_canonical_md``)."""
    lines = ["---"]
    for key in CANONICAL_FRONTMATTER_ORDER:
        if key in frontmatter:
            lines.append(f"{key}: {frontmatter[key]}")
    lines.append("---")
    lines.append("## Ingredients")
    for qty, unit, item in ingredients:
        if unit:
            lines.append(f"- {qty} {unit} {item}")
        else:
            lines.append(f"- {qty} {item}")
    lines.append("## Method")
    for index, step in enumerate(steps, start=1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines) + "\n"


def recompose_document(client: Neo4jClient, doc_id: str) -> str:
    """Reconstruct the canonical.md for ``doc_id`` from the domain graph.

    Raises :class:`app.storage.errors.NotFoundError` when the :Document does
    not exist.
    """
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
                   [(e)-[:HAS_FACT]->(f:Fact)
                    WHERE f.valid_to IS NULL AND f.property = 'qty' | f.value][0] AS qty,
                   [(e)-[:HAS_FACT]->(f:Fact)
                    WHERE f.valid_to IS NULL AND f.property = 'unit' | f.value][0] AS unit
            ORDER BY e.position
            """,
            doc_id=doc_id,
        )
        ingredients = [
            (record["qty"], record["unit"], record["item"])
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

    frontmatter = {
        "title": document.get("title", ""),
        "id": document.get("document_id", document.get("id", "")),
        "lang": document.get("lang", "en"),
        "source_lang": document.get("source_lang", ""),
        "servings": document.get("servings", ""),
        "time_min": document.get("time_min", ""),
        "difficulty": document.get("difficulty", ""),
        "verification_level": document.get("verification_level", "L1"),
        "canonical_version": document.get("canonical_version", 1),
    }
    return _render_canonical_md(frontmatter, ingredients, steps)
