"""Pack rollback helpers (WP-E4, GE4).

A pack rollback re-canonicalizes documents with the previous pack version.
``extract_document`` is idempotent and never deletes, but it overwrites Fact
values in place. To keep the bitemporal history intact (ADR-001 D3), the
rollback procedure must:

1. snapshot the current Facts before re-extract;
2. re-extract with the old pack;
3. call :func:`apply_rollback_versions` to turn the pre-rollback values into
   obsolete ``VERSION_OF`` versions (or invalidate facts that disappeared).

This module provides the snapshot/versioning primitives. The orchestration
(re-canonicalize + re-extract) lives in ``scripts/rollback_pack.py``.
"""
from __future__ import annotations

import uuid
from typing import Any

from app.storage.client import Neo4jClient


def snapshot_document_facts(
    client: Neo4jClient, doc_id: str
) -> dict[str, dict[str, Any]]:
    """Return the current Facts of a Document keyed by logical_id."""
    with client.session() as session:
        records = session.run(
            """
            MATCH (d:Document {id: $doc_id})<-[:PART_OF_DOC]-(e:Entity)
                  -[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL
            RETURN f.logical_id AS logical_id, f.id AS id, f.value AS value,
                   f.valid_from AS valid_from, e.id AS entity_id
            """,
            doc_id=doc_id,
        )
        snapshot: dict[str, dict[str, Any]] = {}
        for record in records:
            logical_id = record["logical_id"] or record["id"]
            snapshot[logical_id] = {
                "value": record["value"],
                "valid_from": record["valid_from"],
                "entity_id": record["entity_id"],
            }
        return snapshot


def apply_rollback_versions(
    client: Neo4jClient,
    doc_id: str,
    snapshot: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Version the pre-rollback Facts after a re-extract with the old pack.

    - Facts whose value changed become obsolete ``VERSION_OF`` predecessors of
      the current (old-pack) Fact.
    - Facts present in the snapshot but absent after re-extract are invalidated
      (``valid_to = now``, ``status = obsolete``), never deleted.
    """
    changes: list[dict[str, Any]] = []
    with client.session() as session:
        current_records = session.run(
            """
            MATCH (d:Document {id: $doc_id})<-[:PART_OF_DOC]-(e:Entity)
                  -[:HAS_FACT]->(f:Fact)
            WHERE f.valid_to IS NULL
            RETURN f.logical_id AS logical_id, f.id AS id, f.value AS value,
                   f.property AS property, f.source_id AS source_id,
                   f.confidence AS confidence, e.id AS entity_id
            """,
            doc_id=doc_id,
        )
        current_by_logical: dict[str, dict[str, Any]] = {}
        for record in current_records:
            logical_id = record["logical_id"] or record["id"]
            current_by_logical[logical_id] = dict(record)

        for logical_id, old in snapshot.items():
            current = current_by_logical.get(logical_id)
            if current is None:
                session.run(
                    """
                    MATCH (f:Fact {logical_id: $logical_id})
                    WHERE f.valid_to IS NULL
                    SET f.valid_to = datetime(), f.status = 'obsolete'
                    """,
                    logical_id=logical_id,
                )
                changes.append({"logical_id": logical_id, "action": "invalidate"})
                continue

            if current["value"] == old["value"]:
                continue

            new_id = f"{logical_id}__rollback_{uuid.uuid4().hex[:12]}"
            session.run(
                """
                MATCH (cur:Fact {id: $cur_id})
                MATCH (e:Entity {id: $entity_id})
                CREATE (old:Fact {id: $new_id})
                SET old.logical_id = $logical_id,
                    old.property = cur.property,
                    old.value = $old_value,
                    old.valid_from = $old_valid_from,
                    old.valid_to = datetime(),
                    old.status = 'obsolete',
                    old.source_id = cur.source_id,
                    old.confidence = cur.confidence
                CREATE (old)-[:VERSION_OF]->(cur)
                CREATE (e)-[:HAS_FACT]->(old)
                """,
                cur_id=current["id"],
                entity_id=old["entity_id"],
                new_id=new_id,
                logical_id=logical_id,
                old_value=old["value"],
                old_valid_from=old["valid_from"],
            )
            changes.append({"logical_id": logical_id, "action": "version"})

    return changes
