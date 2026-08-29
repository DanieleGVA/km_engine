"""Conflict detection for km_engine (WP6, Gate G6).

A conflict is two current Facts (``valid_to IS NULL``, ``status = 'valid'``)
on the same Entity and property with different values and different sources.
Detection can run as a full scan or as a post-ingest hook for a set of
entities. Detected conflicts are inserted into the Postgres ``conflicts``
table with ``status = 'pending'``.

Suggestion rule (documented, Q10):
1. higher confidence wins (EXTRACTED > INFERRED > AMBIGUOUS);
2. on equal confidence, the fact whose Source has the most recent
   ``ingested_at`` wins;
3. on a further tie, choice ``b`` wins.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

from .serialization import row_to_conflict

CONFIDENCE_RANK = {"EXTRACTED": 3, "INFERRED": 2, "AMBIGUOUS": 1}
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _to_datetime(value: Any) -> datetime:
    """Best-effort conversion of Neo4j temporal values to a Python datetime."""
    if value is None:
        return _EPOCH
    if isinstance(value, datetime):
        return value
    to_native = getattr(value, "to_native", None)
    if callable(to_native):
        try:
            native = to_native()
        except Exception:  # noqa: BLE001 - fallback below
            native = None
        if isinstance(native, datetime):
            return native
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        try:
            return datetime.fromisoformat(str(iso()))
        except Exception:  # noqa: BLE001 - fallback below
            return _EPOCH
    return _EPOCH


def _iter_fact_rows(
    client: Neo4jClient, *, entity_id: str | None = None
) -> list[dict[str, Any]]:
    """Return current, valid, sourced Facts (optionally for one Entity)."""

    def work(tx: Any) -> list[dict[str, Any]]:
        if entity_id is not None:
            query = """
            MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL AND f.status = 'valid' AND f.source_id IS NOT NULL
            OPTIONAL MATCH (s:Source {id: f.source_id})
            RETURN e.id AS entity_id, f.property AS property, f.value AS value,
                   f.source_id AS source_id, f.id AS fact_id,
                   f.logical_id AS logical_id, f.confidence AS confidence,
                   s.ingested_at AS source_ingested_at, s.uri AS source_uri
            ORDER BY e.id, f.property, f.source_id, f.value
            """
            params: dict[str, Any] = {"entity_id": entity_id}
        else:
            query = """
            MATCH (e:Entity)-[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL AND f.status = 'valid' AND f.source_id IS NOT NULL
            OPTIONAL MATCH (s:Source {id: f.source_id})
            RETURN e.id AS entity_id, f.property AS property, f.value AS value,
                   f.source_id AS source_id, f.id AS fact_id,
                   f.logical_id AS logical_id, f.confidence AS confidence,
                   s.ingested_at AS source_ingested_at, s.uri AS source_uri
            ORDER BY e.id, f.property, f.source_id, f.value
            """
            params = {}
        result = tx.run(query, **params)
        return [dict(record) for record in result]

    with client.session() as session:
        return session.execute_read(work)


def _suggest_winner(
    fact_a: dict[str, Any], fact_b: dict[str, Any]
) -> tuple[str, str]:
    """Return ``(choice, reason)`` for the documented suggestion rule."""
    rank_a = CONFIDENCE_RANK.get(fact_a.get("confidence"), 0)
    rank_b = CONFIDENCE_RANK.get(fact_b.get("confidence"), 0)
    if rank_a != rank_b:
        if rank_a > rank_b:
            return "a", (
                f"higher confidence ({fact_a.get('confidence')} > "
                f"{fact_b.get('confidence')})"
            )
        return "b", (
            f"higher confidence ({fact_b.get('confidence')} > "
            f"{fact_a.get('confidence')})"
        )

    recency_a = _to_datetime(fact_a.get("source_ingested_at"))
    recency_b = _to_datetime(fact_b.get("source_ingested_at"))
    if recency_a != recency_b:
        if recency_a > recency_b:
            return "a", "more recent source"
        return "b", "more recent source"

    return "b", "sources equally recent (tie-break b)"


def _conflict_pairs(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group Facts by (entity, property) and build deduplicated conflict pairs."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["entity_id"], row["property"]), []).append(row)

    pairs: list[dict[str, Any]] = []
    for (entity_id, property), facts in grouped.items():
        # Collapse identical (value, source) duplicates before pairing.
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for fact in facts:
            unique.setdefault((fact["value"], fact["source_id"]), fact)
        facts = list(unique.values())

        for i in range(len(facts)):
            for j in range(i + 1, len(facts)):
                a, b = facts[i], facts[j]
                if a["value"] == b["value"]:
                    continue
                if a["source_id"] == b["source_id"]:
                    continue
                # Deterministic a/b order: lexicographic by (source_id, value).
                if (a["source_id"], a["value"]) > (b["source_id"], b["value"]):
                    a, b = b, a
                choice, reason = _suggest_winner(a, b)
                pairs.append(
                    {
                        "entity_id": entity_id,
                        "property": property,
                        "value_a": a["value"],
                        "value_b": b["value"],
                        "source_a": a["source_id"],
                        "source_b": b["source_id"],
                        "fact_a": a,
                        "fact_b": b,
                        "suggestion": f"{choice}: {reason}",
                    }
                )
    return pairs


def _pending_exists(conn: psycopg.Connection, pair: dict[str, Any]) -> bool:
    """Return True when the same unordered pair already has a pending conflict."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT id FROM conflicts
            WHERE entity_id = %s AND property = %s AND status = 'pending'
              AND (
                    (value_a = %s AND value_b = %s AND source_a = %s AND source_b = %s)
                 OR (value_a = %s AND value_b = %s AND source_a = %s AND source_b = %s)
              )
            LIMIT 1
            """,
            (
                pair["entity_id"],
                pair["property"],
                pair["value_a"],
                pair["value_b"],
                pair["source_a"],
                pair["source_b"],
                pair["value_b"],
                pair["value_a"],
                pair["source_b"],
                pair["source_a"],
            ),
        )
        return cur.fetchone() is not None


def _insert_conflict(
    conn: psycopg.Connection, pair: dict[str, Any]
) -> dict[str, Any]:
    """Insert a pending conflict row and return its serialized form."""
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO conflicts
                (entity_id, property, value_a, value_b, source_a, source_b, status, suggestion)
            VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s)
            RETURNING id, entity_id, property, value_a, value_b, source_a,
                      source_b, status, suggestion, resolved_by, resolved_at, created_at
            """,
            (
                pair["entity_id"],
                pair["property"],
                pair["value_a"],
                pair["value_b"],
                pair["source_a"],
                pair["source_b"],
                pair["suggestion"],
            ),
        )
        row = cur.fetchone()
    return row_to_conflict(row)


def scan_conflicts(
    repo: GraphRepository,
    conn: psycopg.Connection,
    *,
    entity_id: str | None = None,
) -> list[dict[str, Any]]:
    """Scan the graph and insert new pending conflicts.

    Returns only the conflicts created by this call. Already-open pending
    conflicts for the same unordered pair are skipped (dedup).
    """
    rows = _iter_fact_rows(repo.client, entity_id=entity_id)
    created: list[dict[str, Any]] = []
    for pair in _conflict_pairs(rows):
        if _pending_exists(conn, pair):
            continue
        created.append(_insert_conflict(conn, pair))
    return created


def detect_conflicts_for_entity(
    repo: GraphRepository, conn: psycopg.Connection, entity_id: str
) -> list[dict[str, Any]]:
    """Targeted conflict scan for a single Entity (used by the ingest hook)."""
    return scan_conflicts(repo, conn, entity_id=entity_id)


def post_ingest_hook(
    repo: GraphRepository,
    conn: psycopg.Connection,
    entity_ids: list[str] | tuple[str, ...],
) -> list[dict[str, Any]]:
    """Run conflict detection for the Entities touched by an ingestion chunk."""
    created: list[dict[str, Any]] = []
    for entity_id in entity_ids:
        created.extend(detect_conflicts_for_entity(repo, conn, entity_id))
    return created
