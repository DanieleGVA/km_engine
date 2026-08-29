"""Map graphify extraction output to Entity/Fact/Relation records."""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.ingest.models import (
    EntityRecord,
    ExtractionResult,
    FactRecord,
    FileInfo,
    RelationRecord,
)

_SELF_DROP_RELATIONS = frozenset({"imports", "imports_from", "re_exports"})
_GENERIC_RELATIONS = frozenset({"references", "uses", "mentions"})


def _collapse_relations(
    relations: list[RelationRecord],
) -> list[RelationRecord]:
    """Collapse same-pair relations with graphify's specific-beats-generic rule.

    graphify's undirected build keeps one edge per node pair: a generic
    relation (``references``/``uses``/``mentions``) never overwrites a specific
    one (``calls``/``imports``/...). Replicating that here keeps km_engine's
    code extraction at parity with graphify.
    """
    if len(relations) <= 1:
        return relations
    ordered = sorted(
        relations,
        key=lambda r: (r.source_entity_id, r.target_entity_id, r.relation),
    )
    best: dict[tuple[str, str], RelationRecord] = {}
    for rel in ordered:
        key = (rel.source_entity_id, rel.target_entity_id)
        existing = best.get(key)
        if (
            existing is not None
            and rel.relation in _GENERIC_RELATIONS
            and existing.relation not in _GENERIC_RELATIONS
        ):
            continue
        best[key] = rel
    return [best[k] for k in sorted(best)]


def namespace_for(source_uri: str) -> str:
    """Short stable namespace for a source URI."""
    return hashlib.sha256(source_uri.encode("utf-8")).hexdigest()[:12]


def make_entity_id(namespace: str, node_id: str) -> str:
    """Deterministic, bounded Entity id with the ``wp4_`` test prefix."""
    digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:24]
    return f"wp4_{namespace}_{digest}"


def make_source_id(uri: str) -> str:
    """Deterministic Source id for a file URI."""
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()[:24]
    return f"wp4_src_{digest}"


def _source_id_for_source_file(
    root: Path, source_file: str | None, file_info: dict[str, FileInfo]
) -> str | None:
    if source_file and source_file in file_info:
        return file_info[source_file].source_id
    if source_file:
        candidate = root / source_file
        if candidate.exists():
            return make_source_id(str(candidate))
    return None


def map_extraction(
    result: ExtractionResult,
    *,
    namespace: str,
    root: Path,
    file_info: dict[str, FileInfo],
) -> tuple[list[EntityRecord], list[FactRecord], list[RelationRecord]]:
    """Convert deduplicated graphify nodes/edges into Neo4j records.

    Dangling edges (external imports) are dropped, matching graphify's build
    behaviour. Self import/re-export edges are dropped for the same reason.
    """
    id_map: dict[str, str] = {}
    entities: list[EntityRecord] = []
    facts: list[FactRecord] = []
    relations: list[RelationRecord] = []

    for node in result.nodes:
        node_id = str(node.get("id", ""))
        if not node_id:
            continue
        entity_id = make_entity_id(namespace, node_id)
        id_map[node_id] = entity_id
        label = str(node.get("label") or node_id)
        node_type = node.get("file_type") or "code"
        source_file = node.get("source_file")
        source_location = node.get("source_location")
        confidence = str(node.get("confidence") or "EXTRACTED")
        source_id = _source_id_for_source_file(root, source_file, file_info)
        entities.append(
            EntityRecord(
                entity_id=entity_id,
                label=label,
                type=node_type,
                source_file=source_file,
                source_location=source_location,
                confidence=confidence,
                language="en",
                translation_state="native",
            )
        )
        facts.append(
            FactRecord(
                fact_id=f"{entity_id}__label",
                entity_id=entity_id,
                property="label",
                value=label,
                source_id=source_id,
                confidence=confidence,
                language="en",
                translation_state="native",
            )
        )

    for edge in result.edges:
        src = edge.get("source")
        tgt = edge.get("target")
        if src not in id_map or tgt not in id_map:
            continue
        relation = str(edge.get("relation") or "RELATES_TO")
        if src == tgt and relation in _SELF_DROP_RELATIONS:
            continue
        source_file = edge.get("source_file")
        relations.append(
            RelationRecord(
                source_entity_id=id_map[src],
                target_entity_id=id_map[tgt],
                relation=relation,
                confidence=str(edge.get("confidence") or "EXTRACTED"),
                source_id=_source_id_for_source_file(root, source_file, file_info),
                source_file=source_file,
                source_location=edge.get("source_location"),
            )
        )

    return entities, facts, _collapse_relations(relations)
