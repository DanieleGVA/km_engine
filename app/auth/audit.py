"""Audit log append-only su Postgres (FR5.2, Q9 — ADR-002 D4).

Solo modifiche (CREATE/UPDATE/INVALIDATE/RESOLVE/GRANT_ROLE/...): nessun log
delle query utente. L'inserimento avviene nella transazione del chiamatore:
questo modulo NON committa, cosi' entita' e relativa riga di audit restano
atomiche. Password in chiaro non finiscono mai in old_value/new_value.
"""
from __future__ import annotations

import uuid

import psycopg
from psycopg.types.json import Json


def record(
    conn: psycopg.Connection,
    user_id: uuid.UUID | str | None,
    action: str,
    entity_id: str,
    entity_type: str,
    *,
    old_value: dict | None = None,
    new_value: dict | None = None,
) -> None:
    """Inserisce una riga in audit_log (senza commit: transazione del chiamatore).

    user_id None = azione di sistema (pipeline, migrazione — ADR-002 D4).
    """
    if user_id is not None and not isinstance(user_id, uuid.UUID):
        user_id = uuid.UUID(str(user_id))
    conn.execute(
        """
        INSERT INTO audit_log (user_id, action, entity_id, entity_type, old_value, new_value)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (
            user_id,
            action,
            entity_id,
            entity_type,
            Json(old_value) if old_value is not None else None,
            Json(new_value) if new_value is not None else None,
        ),
    )
