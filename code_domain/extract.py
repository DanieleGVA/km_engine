"""Code-domain extractor: canonical.md -> Neo4j domain graph.

This is the code-domain counterpart of ``app.domain.extract.extract_document``.
It parses the code IR (frontmatter + ``## Functions`` / ``## Classes`` /
``## Dependencies``) and writes:

- one :Document per module (keyed by ``doc_id``);
- one :Entity per function/class/dependency with ``PART_OF_DOC``;
- ``NORMALIZED_TO`` links to the ``CODE-FUNCTION`` / ``CODE-CLASS`` /
  ``CODE-DEPENDENCY`` :CanonicalTerm nodes;
- a :Source node with the canonical.md provenance.

Every write is a MERGE on a deterministic id, so re-extracting the same
``doc_id`` + ``canonical_hash`` is idempotent.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.domain.pack import DomainPackBundle
from app.rag.cache import invalidate_rag_caches
from app.storage.client import Neo4jClient

CODE_FRONTMATTER_ORDER = (
    "title",
    "id",
    "lang",
    "source_lang",
    "verification_level",
    "canonical_version",
)
CODE_SECTIONS = ("Functions", "Classes", "Dependencies")

_SECTION_RE = re.compile(r"^##\s+(Functions|Classes|Dependencies)\s*$")
_BULLET_RE = re.compile(r"^-\s+(.*)$")


@dataclass
class CodeDocumentRecord:
    """Counts produced by one :func:`extract_code_document` run."""

    document_id: str
    canonical_hash: str
    functions: int
    classes: int
    dependencies: int
    terms_linked: int


@dataclass
class ParsedCodeDoc:
    """A parsed code canonical markdown document."""

    frontmatter: dict[str, Any]
    functions: list[str]
    classes: list[str]
    dependencies: list[str]


def _now() -> datetime:
    return datetime.now(UTC)


def _canonical_hash(canonical_md: str) -> str:
    return hashlib.sha256(canonical_md.encode("utf-8")).hexdigest()


def _split_frontmatter(md: str) -> tuple[dict[str, Any], str]:
    import yaml

    lines = md.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing frontmatter: document must start with '---'")
    end: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise ValueError("unterminated frontmatter: missing closing '---'")
    fm = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(fm, dict):
        raise ValueError("frontmatter must be a YAML mapping")
    return fm, "\n".join(lines[end + 1:])


def parse_code_md(md: str) -> ParsedCodeDoc:
    """Parse a code canonical markdown document."""
    frontmatter, body = _split_frontmatter(md)
    for key in ("title", "id", "lang", "source_lang"):
        if key not in frontmatter:
            raise ValueError(f"missing required frontmatter key {key!r}")

    sections: dict[str, list[str]] = {name: [] for name in CODE_SECTIONS}
    current: str | None = None
    for line in body.splitlines():
        match = _SECTION_RE.match(line.strip())
        if match:
            current = match.group(1)
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line.strip())
        if bullet:
            sections[current].append(bullet.group(1).strip())

    return ParsedCodeDoc(
        frontmatter=frontmatter,
        functions=sections["Functions"],
        classes=sections["Classes"],
        dependencies=sections["Dependencies"],
    )


def _term_id(pack: DomainPackBundle, entry_id: str) -> str | None:
    """Return the ``namespace:term_id`` graph id for a glossary entry."""
    for namespace in ("tecnica", "ingredienti", "stati"):
        glossary = getattr(pack.glossaries, namespace)
        for entry in glossary.entries:
            if entry.id == entry_id:
                return f"{namespace}:{entry.id}"
    return None


def extract_code_document(
    client: Neo4jClient,
    doc_id: str,
    canonical_md: str,
    pack: DomainPackBundle,
) -> CodeDocumentRecord:
    """Extract a code canonical.md into the Neo4j domain graph (idempotent)."""
    parsed = parse_code_md(canonical_md)
    frontmatter = parsed.frontmatter
    digest = _canonical_hash(canonical_md)
    now = _now()

    document_id = str(frontmatter.get("id", doc_id))
    title = str(frontmatter.get("title", ""))
    lang = str(frontmatter.get("lang", "en"))
    source_lang = str(frontmatter.get("source_lang", "en"))
    verification_level = str(frontmatter.get("verification_level", "L1"))
    canonical_version = int(frontmatter.get("canonical_version", 1))
    source_id = f"{doc_id}:source"
    source_uri = f"code://{document_id}"
    pack_id = f"{pack.pack.name}:{pack.pack.version}"

    function_term = _term_id(pack, "CODE-FUNCTION")
    class_term = _term_id(pack, "CODE-CLASS")
    dependency_term = _term_id(pack, "CODE-DEPENDENCY")

    function_rows = [
        {"entity_id": f"{doc_id}:fn:{index}", "label": label, "position": index}
        for index, label in enumerate(parsed.functions)
    ]
    class_rows = [
        {"entity_id": f"{doc_id}:cls:{index}", "label": label, "position": index}
        for index, label in enumerate(parsed.classes)
    ]
    dependency_rows = [
        {"entity_id": f"{doc_id}:dep:{index}", "label": label, "position": index}
        for index, label in enumerate(parsed.dependencies)
    ]

    def work(tx: Any) -> None:
        tx.run(
            """
            MERGE (d:Document {id: $doc_id})
            SET d.document_id = $document_id,
                d.title = $title,
                d.lang = $lang,
                d.source_lang = $source_lang,
                d.source_language = $source_lang,
                d.canonical_hash = $hash,
                d.verification_level = $verification_level,
                d.translation_state = 'native',
                d.canonical_version = $canonical_version,
                d.is_public = false,
                d.roles = [],
                d.teams = []
            """,
            doc_id=doc_id,
            document_id=document_id,
            title=title,
            lang=lang,
            source_lang=source_lang,
            hash=digest,
            verification_level=verification_level,
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

        for row in function_rows:
            _write_entity(tx, row, "function", doc_id, source_uri, function_term)
        for row in class_rows:
            _write_entity(tx, row, "class", doc_id, source_uri, class_term)
        for row in dependency_rows:
            _write_entity(tx, row, "dependency", doc_id, source_uri, dependency_term)

    with client.session() as session:
        session.execute_write(work)

    invalidate_rag_caches()

    return CodeDocumentRecord(
        document_id=doc_id,
        canonical_hash=digest,
        functions=len(function_rows),
        classes=len(class_rows),
        dependencies=len(dependency_rows),
        terms_linked=len(function_rows) + len(class_rows) + len(dependency_rows),
    )


def _write_entity(
    tx: Any,
    row: dict[str, Any],
    entity_type: str,
    doc_id: str,
    source_uri: str,
    term_id: str | None,
) -> None:
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
        type=entity_type,
        position=row["position"],
        source_uri=source_uri,
        doc_id=doc_id,
    )
    if term_id is not None:
        tx.run(
            """
            MATCH (e:Entity {id: $entity_id})
            MATCH (t:CanonicalTerm {id: $term_id})
            MERGE (e)-[:NORMALIZED_TO]->(t)
            """,
            entity_id=row["entity_id"],
            term_id=term_id,
        )
