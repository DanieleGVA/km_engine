"""Row serialization for the ``conflicts`` Postgres table."""
from __future__ import annotations

from typing import Any


def row_to_conflict(row: dict[str, Any]) -> dict[str, Any]:
    """Convert a ``dict_row`` conflict row to a JSON-friendly dict."""
    return {
        "id": row["id"],
        "entity_id": row["entity_id"],
        "property": row["property"],
        "value_a": row["value_a"],
        "value_b": row["value_b"],
        "source_a": row["source_a"],
        "source_b": row["source_b"],
        "status": row["status"],
        "suggestion": row["suggestion"],
        "resolved_by": str(row["resolved_by"]) if row["resolved_by"] is not None else None,
        "resolved_at": row["resolved_at"].isoformat() if row["resolved_at"] is not None else None,
        "created_at": row["created_at"].isoformat() if row["created_at"] is not None else None,
    }
