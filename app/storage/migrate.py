"""One-time graph.json -> Neo4j migration (ADR-001 D6).

Reads a graphify node-link JSON file and writes Entity nodes and RELATES_TO
arcs with MERGE, in chunked transactions. The script is idempotent: running
it twice with the same input produces the same graph state.

Mapping notes:
- graphify node -> :Entity (id, label, type=file_type, source_file,
  source_location, confidence). graphify nodes have no confidence, so the
  default is EXTRACTED (they are directly extracted entities).
- graphify link -> :RELATES_TO (relation, confidence, status='valid',
  valid_from=migration time). source_file/source_location are kept on the arc
  for provenance even though they are not in the baseline schema comment.
- visibility default deny: is_public=false, roles=[], teams=[].
- original source files are re-registered as :Source with uri and hash. The
  graph.json format has no content hash, so the hash is a stable SHA-256 of
  the URI (placeholder until WP4 re-ingests content).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.storage.client import Neo4jClient
from app.storage.errors import ValidationError


@dataclass(frozen=True)
class MigrationReport:
    """Counts produced by a migration run."""

    nodes_read: int
    links_read: int
    entities_written: int
    relations_written: int
    sources_written: int


def _now() -> datetime:
    return datetime.now(UTC)


def _chunks(rows: Sequence[dict[str, Any]], size: int) -> Iterator[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def _source_id(uri: str) -> str:
    digest = hashlib.sha256(uri.encode("utf-8")).hexdigest()
    return f"src_{digest}"


def _node_rows(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for node in nodes:
        node_id = str(node.get("id", ""))
        if not node_id:
            raise ValidationError("graph.json contains a node without id")
        rows.append(
            {
                "id": node_id,
                "label": node.get("label") or node_id,
                "type": node.get("file_type") or node.get("type"),
                "source_file": node.get("source_file"),
                "source_location": node.get("source_location"),
                "confidence": node.get("confidence") or "EXTRACTED",
            }
        )
    return rows


def _link_rows(links: list[dict[str, Any]], valid_from: datetime) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for link in links:
        source = link.get("source", link.get("_src"))
        target = link.get("target", link.get("_tgt"))
        if source is None or target is None:
            raise ValidationError("graph.json contains a link without source/target")
        rows.append(
            {
                "source": str(source),
                "target": str(target),
                "relation": link.get("relation") or "RELATES_TO",
                "confidence": link.get("confidence") or "EXTRACTED",
                "source_file": link.get("source_file"),
                "source_location": link.get("source_location"),
                "valid_from": valid_from,
            }
        )
    return rows


def _source_rows(
    nodes: list[dict[str, Any]], links: list[dict[str, Any]], ingested_at: datetime
) -> list[dict[str, Any]]:
    uris: set[str] = set()
    for node in nodes:
        uri = node.get("source_file")
        if uri:
            uris.add(str(uri))
    for link in links:
        uri = link.get("source_file")
        if uri:
            uris.add(str(uri))
    return [
        {
            "id": _source_id(uri),
            "uri": uri,
            "type": "file",
            "hash": hashlib.sha256(uri.encode("utf-8")).hexdigest(),
            "ingested_at": ingested_at,
        }
        for uri in sorted(uris)
    ]


def _write_entities(client: Neo4jClient, rows: list[dict[str, Any]]) -> None:
    def work(tx: Any) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (e:Entity {id: row.id})
            SET e.label = row.label,
                e.type = row.type,
                e.source_file = row.source_file,
                e.source_location = row.source_location,
                e.confidence = row.confidence,
                e.is_public = false,
                e.roles = [],
                e.teams = []
            """,
            rows=rows,
        )

    with client.session() as session:
        session.execute_write(work)


def _write_links(client: Neo4jClient, rows: list[dict[str, Any]]) -> None:
    def work(tx: Any) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (a:Entity {id: row.source})
            MATCH (b:Entity {id: row.target})
            MERGE (a)-[r:RELATES_TO {relation: row.relation}]->(b)
            SET r.confidence = row.confidence,
                r.status = 'valid',
                r.valid_from = row.valid_from,
                r.source_file = row.source_file,
                r.source_location = row.source_location,
                r.valid_to = null
            """,
            rows=rows,
        )

    with client.session() as session:
        session.execute_write(work)


def _write_sources(client: Neo4jClient, rows: list[dict[str, Any]]) -> None:
    def work(tx: Any) -> None:
        tx.run(
            """
            UNWIND $rows AS row
            MERGE (s:Source {id: row.id})
            SET s.uri = row.uri,
                s.type = row.type,
                s.hash = row.hash,
                s.ingested_at = row.ingested_at
            """,
            rows=rows,
        )

    with client.session() as session:
        session.execute_write(work)


def migrate_graphjson(
    path: str | Path,
    client: Neo4jClient | None = None,
    *,
    chunk_size: int = 500,
    register_sources: bool = True,
    migration_time: datetime | None = None,
) -> MigrationReport:
    """Migrate a graphify node-link JSON file into Neo4j.

    ``client`` may be supplied by the caller; otherwise a client is created
    from KM_NEO4J_* env vars and closed before returning.
    """
    if chunk_size < 1:
        raise ValidationError("chunk_size must be >= 1")
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    nodes = data.get("nodes", [])
    links = data.get("links", data.get("edges", []))
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ValidationError("graph.json must contain 'nodes' and 'links' lists")

    ts = migration_time or _now()
    entity_rows = _node_rows(nodes)
    link_rows = _link_rows(links, ts)
    source_rows = _source_rows(nodes, links, ts) if register_sources else []

    owns_client = client is None
    if client is None:
        client = Neo4jClient.from_env()
    try:
        client.verify_connectivity()
        for chunk in _chunks(entity_rows, chunk_size):
            _write_entities(client, chunk)
        for chunk in _chunks(link_rows, chunk_size):
            _write_links(client, chunk)
        for chunk in _chunks(source_rows, chunk_size):
            _write_sources(client, chunk)
    finally:
        if owns_client:
            client.close()

    return MigrationReport(
        nodes_read=len(nodes),
        links_read=len(links),
        entities_written=len(entity_rows),
        relations_written=len(link_rows),
        sources_written=len(source_rows),
    )
