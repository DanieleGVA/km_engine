"""Code-domain recomposer: Neo4j domain graph -> canonical.md.

This is the code-domain counterpart of ``app.domain.recompose.recompose_document``.
It reads the :Document node and the function/class/dependency :Entity nodes and
renders the code IR byte-for-byte (same frontmatter order, section separators,
bullet format and single trailing newline as :func:`code_domain.mapping.render_canonical_md`).
"""
from __future__ import annotations

from typing import Any

from app.storage.client import Neo4jClient
from app.storage.errors import NotFoundError

from code_domain.extract import CODE_FRONTMATTER_ORDER


def _render_code_md(
    frontmatter: dict[str, Any],
    functions: list[str],
    classes: list[str],
    dependencies: list[str],
) -> str:
    lines = ["---"]
    for key in CODE_FRONTMATTER_ORDER:
        if key in frontmatter:
            lines.append(f"{key}: {frontmatter[key]}")
    lines.append("---")
    lines.append("## Functions")
    lines.extend(f"- {label}" for label in functions)
    lines.append("## Classes")
    lines.extend(f"- {label}" for label in classes)
    lines.append("## Dependencies")
    lines.extend(f"- {label}" for label in dependencies)
    return "\n".join(lines) + "\n"


def recompose_code_document(client: Neo4jClient, doc_id: str) -> str:
    """Reconstruct the code canonical.md for ``doc_id`` from the domain graph."""
    with client.session() as session:
        document_record = session.run(
            "MATCH (d:Document {id: $doc_id}) RETURN d",
            doc_id=doc_id,
        ).single()
        if document_record is None:
            raise NotFoundError(f"Document {doc_id!r} not found")
        document = dict(document_record["d"])

        function_records = session.run(
            """
            MATCH (e:Entity {type: 'function'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            RETURN e.label AS label
            ORDER BY e.position
            """,
            doc_id=doc_id,
        )
        functions = [record["label"] for record in function_records]

        class_records = session.run(
            """
            MATCH (e:Entity {type: 'class'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            RETURN e.label AS label
            ORDER BY e.position
            """,
            doc_id=doc_id,
        )
        classes = [record["label"] for record in class_records]

        dependency_records = session.run(
            """
            MATCH (e:Entity {type: 'dependency'})-[:PART_OF_DOC]->(d:Document {id: $doc_id})
            RETURN e.label AS label
            ORDER BY e.position
            """,
            doc_id=doc_id,
        )
        dependencies = [record["label"] for record in dependency_records]

    frontmatter = {
        "title": document.get("title", ""),
        "id": document.get("document_id", document.get("id", "")),
        "lang": document.get("lang", "en"),
        "source_lang": document.get("source_lang", "en"),
        "verification_level": document.get("verification_level", "L1"),
        "canonical_version": document.get("canonical_version", 1),
    }
    return _render_code_md(frontmatter, functions, classes, dependencies)
