"""Conflict resolution workflow (WP6, Gate G6).

``approve`` applies the chosen value by invalidating the losing Fact through
``GraphRepository.invalidate_fact`` and marks the row ``approved``.
``reject`` marks the row ``rejected`` without touching the graph.
Both write an audit row (FR5.2) in the same Postgres transaction as the
status change.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.auth import record_audit
from app.storage.repository import GraphRepository

from .errors import (
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    ConflictResolutionError,
    InvalidChoiceError,
)
from .serialization import row_to_conflict

VALID_STATUSES = {"pending", "approved", "rejected"}


def _now() -> datetime:
    return datetime.now(UTC)


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _get_row(conn: psycopg.Connection, conflict_id: int) -> dict[str, Any] | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM conflicts WHERE id = %s", (conflict_id,))
        return cur.fetchone()


def get_conflict(conn: psycopg.Connection, conflict_id: int) -> dict[str, Any] | None:
    """Return a conflict by id, or None."""
    row = _get_row(conn, conflict_id)
    return row_to_conflict(row) if row else None


def list_conflicts(
    conn: psycopg.Connection, *, status: str | None = None
) -> list[dict[str, Any]]:
    """List conflicts, optionally filtered by status."""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
        )
    with conn.cursor(row_factory=dict_row) as cur:
        if status is None:
            cur.execute("SELECT * FROM conflicts ORDER BY id")
        else:
            cur.execute(
                "SELECT * FROM conflicts WHERE status = %s ORDER BY id", (status,)
            )
        rows = cur.fetchall()
    return [row_to_conflict(row) for row in rows]


def _find_current_fact(
    repo: GraphRepository,
    *,
    entity_id: str,
    property: str,
    value: str,
    source_id: str,
) -> dict[str, Any] | None:
    """Find the current Fact matching the losing side of a conflict."""

    def work(tx: Any) -> dict[str, Any] | None:
        record = tx.run(
            """
            MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL
              AND f.property = $property
              AND f.value = $value
              AND f.source_id = $source_id
            RETURN f
            ORDER BY f.valid_from DESC
            LIMIT 1
            """,
            entity_id=entity_id,
            property=property,
            value=value,
            source_id=source_id,
        ).single()
        return dict(record["f"]) if record else None

    with repo.client.session() as session:
        return session.execute_read(work)


def approve_conflict(
    repo: GraphRepository,
    conn: psycopg.Connection,
    conflict_id: int,
    choice: str,
    user_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Approve side ``a`` or ``b`` of a pending conflict.

    The losing Fact is invalidated in Neo4j (status=obsolete, valid_to=now);
    the winning Fact stays current. The Postgres row becomes ``approved``.
    """
    if choice not in ("a", "b"):
        raise InvalidChoiceError(f"choice must be 'a' or 'b', got {choice!r}")

    row = _get_row(conn, conflict_id)
    if row is None:
        raise ConflictNotFoundError(f"Conflict {conflict_id!r} not found")
    if row["status"] != "pending":
        raise ConflictAlreadyResolvedError(
            f"Conflict {conflict_id!r} is already {row['status']}"
        )

    if choice == "a":
        win_value = row["value_a"]
        lose_value, lose_source = row["value_b"], row["source_b"]
    else:
        win_value = row["value_b"]
        lose_value, lose_source = row["value_a"], row["source_a"]

    losing_fact = _find_current_fact(
        repo,
        entity_id=row["entity_id"],
        property=row["property"],
        value=lose_value,
        source_id=lose_source,
    )
    if losing_fact is None:
        raise ConflictResolutionError(
            f"Losing fact for conflict {conflict_id!r} not found in the graph; "
            "it may already be invalidated"
        )

    fact_id = losing_fact.get("logical_id") or losing_fact.get("id")
    repo.invalidate_fact(fact_id, author_id=str(user_id))

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE conflicts
                SET status = 'approved', resolved_by = %s, resolved_at = %s
                WHERE id = %s
                RETURNING id, entity_id, property, value_a, value_b, source_a,
                          source_b, status, suggestion, resolved_by, resolved_at, created_at
                """,
                (_as_uuid(user_id), _now(), conflict_id),
            )
            updated = cur.fetchone()
        record_audit(
            conn,
            user_id,
            "RESOLVE",
            str(conflict_id),
            "Conflict",
            old_value={"status": "pending"},
            new_value={
                "status": "approved",
                "choice": choice,
                "winning_value": win_value,
                "losing_value": lose_value,
            },
        )
    return row_to_conflict(updated)


def reject_conflict(
    conn: psycopg.Connection,
    conflict_id: int,
    user_id: uuid.UUID | str,
) -> dict[str, Any]:
    """Reject a pending conflict without modifying the graph."""
    row = _get_row(conn, conflict_id)
    if row is None:
        raise ConflictNotFoundError(f"Conflict {conflict_id!r} not found")
    if row["status"] != "pending":
        raise ConflictAlreadyResolvedError(
            f"Conflict {conflict_id!r} is already {row['status']}"
        )

    with conn.transaction():
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE conflicts
                SET status = 'rejected', resolved_by = %s, resolved_at = %s
                WHERE id = %s
                RETURNING id, entity_id, property, value_a, value_b, source_a,
                          source_b, status, suggestion, resolved_by, resolved_at, created_at
                """,
                (_as_uuid(user_id), _now(), conflict_id),
            )
            updated = cur.fetchone()
        record_audit(
            conn,
            user_id,
            "RESOLVE",
            str(conflict_id),
            "Conflict",
            old_value={"status": "pending"},
            new_value={"status": "rejected"},
        )
    return row_to_conflict(updated)
