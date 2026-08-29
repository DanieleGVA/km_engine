"""Truth-maintenance implementation (WP6, Gate G7).

``invalidate_source`` closes every current Fact linked to a Source through
``DERIVED_FROM`` (status=obsolete, valid_to=now) and then propagates to
dependent Facts using the rule documented in ``app/invalidation/__init__.py``.

The graph write for direct invalidation uses ``GraphRepository.invalidate_fact``
(ADR-001 D3: close the interval, never delete). Dependent Facts are marked
``under_review`` with ``GraphRepository.update_fact``, which creates a new
version and keeps the previous one in the ``VERSION_OF`` chain.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg

from app.auth import record_audit
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

from .errors import InvalidationError, SourceNotFoundError

DEFAULT_MAX_DEPTH = 3
MAX_DEPTH_LIMIT = 10


def _now() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _source_exists(client: Neo4jClient, source_id: str) -> bool:
    with client.session() as session:
        record = session.run(
            "MATCH (s:Source {id: $id}) RETURN s.id AS id", id=source_id
        ).single()
        return record is not None


def _current_facts_derived_from(
    client: Neo4jClient, source_id: str
) -> list[dict[str, Any]]:
    """Return current Facts linked to a Source through DERIVED_FROM."""

    def work(tx: Any) -> list[dict[str, Any]]:
        result = tx.run(
            """
            MATCH (f:Fact)-[:DERIVED_FROM]->(s:Source {id: $source_id})
            WHERE f.valid_to IS NULL
            RETURN f
            ORDER BY f.logical_id, f.valid_from DESC
            """,
            source_id=source_id,
        )
        return [dict(record["f"]) for record in result]

    with client.session() as session:
        return session.execute_read(work)


def _entity_id_for_fact(client: Neo4jClient, fact_node_id: str) -> str | None:
    with client.session() as session:
        record = session.run(
            """
            MATCH (e:Entity)-[:HAS_FACT]->(f:Fact {id: $id})
            RETURN e.id AS entity_id
            """,
            id=fact_node_id,
        ).single()
        return record["entity_id"] if record else None


def _dependent_facts_for_parent(
    client: Neo4jClient, parent_logical_id: str
) -> list[dict[str, Any]]:
    """Return current INFERRED Facts that depend on a parent logical Fact.

    Dependency edges (documented in ``app/invalidation/__init__.py``):
    1. explicit ``(d:Fact)-[:DERIVED_FROM]->(p:Fact)`` fact-to-fact derivation;
    2. same-Entity fallback for INFERRED Facts without an explicit edge.
    """

    def work(tx: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        result = tx.run(
            """
            MATCH (d:Fact)-[:DERIVED_FROM]->(p:Fact)
            WHERE (p.logical_id = $parent_logical_id OR p.id = $parent_logical_id)
              AND d.valid_to IS NULL
              AND d.confidence = 'INFERRED'
            OPTIONAL MATCH (e:Entity)-[:HAS_FACT]->(d)
            RETURN d, e.id AS entity_id
            """,
            parent_logical_id=parent_logical_id,
        )
        for record in result:
            rows.append(
                {"fact": dict(record["d"]), "entity_id": record["entity_id"]}
            )

        result = tx.run(
            """
            MATCH (e:Entity)-[:HAS_FACT]->(p:Fact)
            WHERE p.logical_id = $parent_logical_id OR p.id = $parent_logical_id
            MATCH (e)-[:HAS_FACT]->(d:Fact)
            WHERE d.valid_to IS NULL
              AND d.confidence = 'INFERRED'
              AND d.logical_id <> $parent_logical_id
              AND d.id <> $parent_logical_id
            RETURN d, e.id AS entity_id
            """,
            parent_logical_id=parent_logical_id,
        )
        for record in result:
            rows.append(
                {"fact": dict(record["d"]), "entity_id": record["entity_id"]}
            )
        return rows

    with client.session() as session:
        return session.execute_read(work)


def _mark_source_invalidated(
    client: Neo4jClient, source_id: str, reason: str, user_id: str
) -> None:
    """Record invalidation metadata on the Source node (traceability)."""

    def work(tx: Any) -> None:
        tx.run(
            """
            MATCH (s:Source {id: $id})
            SET s.invalidated_at = $now,
                s.invalidation_reason = $reason,
                s.invalidated_by = $user_id
            """,
            id=source_id,
            now=_now(),
            reason=reason,
            user_id=user_id,
        )

    with client.session() as session:
        session.execute_write(work)


def _propagate(
    repo: GraphRepository,
    invalidated: list[dict[str, Any]],
    user_id: str,
    max_depth: int,
) -> list[dict[str, Any]]:
    """Mark dependent INFERRED Facts under_review, level by level."""
    under_review: list[dict[str, Any]] = []
    seen: set[str] = {item["fact_id"] for item in invalidated}
    current_level = invalidated

    for _depth in range(1, max_depth + 1):
        next_level: list[dict[str, Any]] = []
        for parent in current_level:
            for dependent in _dependent_facts_for_parent(
                repo.client, parent["fact_id"]
            ):
                fact = dependent["fact"]
                logical_id = fact.get("logical_id") or fact.get("id")
                if logical_id in seen:
                    continue
                new_fact = repo.update_fact(
                    logical_id, status="under_review", author_id=user_id
                )
                seen.add(logical_id)
                entry = {
                    "fact_id": logical_id,
                    "entity_id": dependent.get("entity_id"),
                    "node_id": new_fact.get("id"),
                }
                under_review.append(entry)
                next_level.append(entry)
        if not next_level:
            break
        current_level = next_level

    return under_review


def invalidate_source(
    repo: GraphRepository,
    conn: psycopg.Connection,
    source_id: str,
    *,
    reason: str,
    user_id: uuid.UUID | str,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Invalidate a Source and propagate to dependent Facts.

    Returns a summary with the invalidated and under-review Fact ids.
    """
    if max_depth is None:
        max_depth = DEFAULT_MAX_DEPTH
    if max_depth < 0 or max_depth > MAX_DEPTH_LIMIT:
        raise InvalidationError(
            f"max_depth must be between 0 and {MAX_DEPTH_LIMIT}, got {max_depth}"
        )
    if not reason or not reason.strip():
        raise InvalidationError("reason must be a non-empty string")

    if not _source_exists(repo.client, source_id):
        raise SourceNotFoundError(f"Source {source_id!r} not found")

    direct = _current_facts_derived_from(repo.client, source_id)
    invalidated: list[dict[str, Any]] = []
    seen_direct: set[str] = set()
    for fact in direct:
        fact_id = fact.get("logical_id") or fact.get("id")
        if fact_id in seen_direct:
            continue
        seen_direct.add(fact_id)
        entity_id = _entity_id_for_fact(repo.client, fact.get("id"))
        repo.invalidate_fact(fact_id, author_id=str(user_id))
        invalidated.append(
            {
                "fact_id": fact_id,
                "entity_id": entity_id,
                "node_id": fact.get("id"),
            }
        )

    under_review = _propagate(repo, invalidated, str(user_id), max_depth)
    _mark_source_invalidated(repo.client, source_id, reason, str(user_id))

    with conn.transaction():
        record_audit(
            conn,
            user_id,
            "INVALIDATE_SOURCE",
            source_id,
            "Source",
            old_value=None,
            new_value={
                "reason": reason,
                "invalidated_facts": [item["fact_id"] for item in invalidated],
                "under_review_facts": [item["fact_id"] for item in under_review],
                "max_depth": max_depth,
            },
        )

    return {
        "source_id": source_id,
        "reason": reason,
        "invalidated_facts": [item["fact_id"] for item in invalidated],
        "under_review_facts": [item["fact_id"] for item in under_review],
        "max_depth": max_depth,
    }
