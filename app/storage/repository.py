"""Neo4j graph repository: CRUD, bitemporal versioning and visibility.

This module is the only application writer of the graph (ADR-001 D1).
Facts are versioned with version-nodes + VERSION_OF; there is no
application-level DELETE. Entity updates are in-place because the
bitemporal model applies to Facts and RELATES_TO arcs (ADR-001 D3).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from neo4j import ManagedTransaction

from app.storage.client import Neo4jClient
from app.storage.errors import AlreadyExistsError, NotFoundError, ValidationError
from app.storage.visibility import Visibility, apply_visibility

VALID_STATUSES = {"valid", "obsolete", "under_review"}
VALID_CONFIDENCES = {"EXTRACTED", "INFERRED", "AMBIGUOUS"}

_UNSET = object()


def _now() -> datetime:
    """Return the current UTC time."""
    return datetime.now(UTC)


def _without_none(props: dict[str, Any]) -> dict[str, Any]:
    """Drop None values because Neo4j cannot store null properties."""
    return {k: v for k, v in props.items() if v is not None}


class GraphRepository:
    """CRUD and versioning operations on the Neo4j knowledge graph."""

    def __init__(self, client: Neo4jClient) -> None:
        self.client = client

    # ------------------------------------------------------------------ helpers
    def _write(self, fn: Callable[[ManagedTransaction], Any]) -> Any:
        with self.client.session() as session:
            return session.execute_write(fn)

    def _read(self, fn: Callable[[ManagedTransaction], Any]) -> Any:
        with self.client.session() as session:
            return session.execute_read(fn)

    @staticmethod
    def _node_dict(node: Any) -> dict[str, Any]:
        data = dict(node)
        # ``valid_to`` is omitted in Neo4j while the interval is open; expose
        # it as None so callers can rely on the key for current versions.
        if "Fact" in node.labels and "valid_to" not in data:
            data["valid_to"] = None
        return data

    @staticmethod
    def _rel_dict(rel: Any, source_id: str, target_id: str) -> dict[str, Any]:
        data = dict(rel)
        data["source_id"] = source_id
        data["target_id"] = target_id
        if "valid_to" not in data:
            data["valid_to"] = None
        return data

    @staticmethod
    def _validate_status(status: str) -> None:
        if status not in VALID_STATUSES:
            raise ValidationError(
                f"invalid status {status!r}; expected one of {sorted(VALID_STATUSES)}"
            )

    @staticmethod
    def _validate_confidence(confidence: str) -> None:
        if confidence not in VALID_CONFIDENCES:
            raise ValidationError(
                f"invalid confidence {confidence!r}; expected one of {sorted(VALID_CONFIDENCES)}"
            )

    @staticmethod
    def _resolve_logical_id(tx: ManagedTransaction, fact_id: str) -> str | None:
        row = tx.run(
            """
            MATCH (f:Fact)
            WHERE f.id = $fact_id OR f.logical_id = $fact_id
            RETURN f.logical_id AS logical_id
            LIMIT 1
            """,
            fact_id=fact_id,
        ).single()
        if row is None:
            return None
        return row["logical_id"] or fact_id

    # ------------------------------------------------------------------ Entity
    def create_entity(
        self,
        *,
        entity_id: str,
        label: str,
        type: str | None = None,
        source_file: str | None = None,
        source_location: str | None = None,
        confidence: str = "EXTRACTED",
        visibility: Visibility | None = None,
    ) -> dict[str, Any]:
        """Create an Entity node. Raises AlreadyExistsError on duplicate id."""
        self._validate_confidence(confidence)
        props = _without_none(
            {
                "label": label,
                "type": type,
                "source_file": source_file,
                "source_location": source_location,
                "confidence": confidence,
            }
        )
        if visibility is not None:
            props.update(visibility.to_props())

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            existing = tx.run(
                "MATCH (e:Entity {id: $id}) RETURN e.id AS id", id=entity_id
            ).single()
            if existing is not None:
                raise AlreadyExistsError(f"Entity {entity_id!r} already exists")
            record = tx.run(
                "CREATE (e:Entity {id: $id}) SET e += $props RETURN e",
                id=entity_id,
                props=props,
            ).single()
            return self._node_dict(record["e"])

        return self._write(work)

    def get_entity(self, entity_id: str) -> dict[str, Any] | None:
        """Return an Entity by id, or None when missing."""

        def work(tx: ManagedTransaction) -> dict[str, Any] | None:
            record = tx.run(
                "MATCH (e:Entity {id: $id}) RETURN e", id=entity_id
            ).single()
            return self._node_dict(record["e"]) if record else None

        return self._read(work)

    def update_entity(
        self,
        entity_id: str,
        *,
        label: Any = _UNSET,
        type: Any = _UNSET,
        source_file: Any = _UNSET,
        source_location: Any = _UNSET,
        confidence: Any = _UNSET,
        visibility: Any = _UNSET,
    ) -> dict[str, Any]:
        """Update Entity properties in place (no versioning for Entity)."""
        if confidence is not _UNSET:
            self._validate_confidence(confidence)

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            record = tx.run(
                "MATCH (e:Entity {id: $id}) RETURN e", id=entity_id
            ).single()
            if record is None:
                raise NotFoundError(f"Entity {entity_id!r} not found")
            props = self._node_dict(record["e"])
            props.pop("id", None)
            if label is not _UNSET:
                props["label"] = label
            if type is not _UNSET:
                if type is None:
                    props.pop("type", None)
                else:
                    props["type"] = type
            if source_file is not _UNSET:
                if source_file is None:
                    props.pop("source_file", None)
                else:
                    props["source_file"] = source_file
            if source_location is not _UNSET:
                if source_location is None:
                    props.pop("source_location", None)
                else:
                    props["source_location"] = source_location
            if confidence is not _UNSET:
                props["confidence"] = confidence
            if visibility is not _UNSET:
                props = apply_visibility(props, visibility)
            result = tx.run(
                "MATCH (e:Entity {id: $id}) SET e += $props RETURN e",
                id=entity_id,
                props=props,
            ).single()
            return self._node_dict(result["e"])

        return self._write(work)

    # -------------------------------------------------------------------- Fact
    def create_fact(
        self,
        *,
        fact_id: str,
        entity_id: str,
        property: str,
        value: str,
        source_id: str | None = None,
        author_id: str | None = None,
        confidence: str = "EXTRACTED",
        status: str = "valid",
        source_valid_from: datetime | None = None,
        source_valid_to: datetime | None = None,
        visibility: Visibility | None = None,
        valid_from: datetime | None = None,
    ) -> dict[str, Any]:
        """Create the first version of a Fact and link it to its Entity."""
        self._validate_confidence(confidence)
        self._validate_status(status)
        props = _without_none(
            {
                "logical_id": fact_id,
                "property": property,
                "value": value,
                "valid_from": valid_from or _now(),
                "source_valid_from": source_valid_from,
                "source_valid_to": source_valid_to,
                "source_id": source_id,
                "author_id": author_id,
                "confidence": confidence,
                "status": status,
            }
        )
        if visibility is not None:
            props.update(visibility.to_props())

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            entity = tx.run(
                "MATCH (e:Entity {id: $id}) RETURN e", id=entity_id
            ).single()
            if entity is None:
                raise NotFoundError(f"Entity {entity_id!r} not found")
            existing = tx.run(
                "MATCH (f:Fact {id: $id}) RETURN f.id AS id", id=fact_id
            ).single()
            if existing is not None:
                raise AlreadyExistsError(f"Fact {fact_id!r} already exists")
            record = tx.run(
                """
                MATCH (e:Entity {id: $entity_id})
                CREATE (f:Fact {id: $fact_id})
                SET f += $props
                CREATE (e)-[:HAS_FACT]->(f)
                RETURN f
                """,
                entity_id=entity_id,
                fact_id=fact_id,
                props=props,
            ).single()
            return self._node_dict(record["f"])

        return self._write(work)

    def get_fact(self, fact_id: str) -> dict[str, Any] | None:
        """Return the current version (valid_to IS NULL) of a logical Fact."""

        def work(tx: ManagedTransaction) -> dict[str, Any] | None:
            logical_id = self._resolve_logical_id(tx, fact_id)
            if logical_id is None:
                return None
            record = tx.run(
                """
                MATCH (f:Fact {logical_id: $logical_id})
                WHERE f.valid_to IS NULL
                RETURN f
                ORDER BY f.valid_from DESC
                LIMIT 1
                """,
                logical_id=logical_id,
            ).single()
            return self._node_dict(record["f"]) if record else None

        return self._read(work)

    def get_fact_history(self, fact_id: str) -> list[dict[str, Any]]:
        """Return all versions of a logical Fact ordered by valid_from."""

        def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
            logical_id = self._resolve_logical_id(tx, fact_id)
            if logical_id is None:
                return []
            result = tx.run(
                """
                MATCH (f:Fact {logical_id: $logical_id})
                RETURN f
                ORDER BY f.valid_from ASC
                """,
                logical_id=logical_id,
            )
            return [self._node_dict(record["f"]) for record in result]

        return self._read(work)

    def update_fact(
        self,
        fact_id: str,
        *,
        value: Any = _UNSET,
        property: Any = _UNSET,
        confidence: Any = _UNSET,
        status: Any = _UNSET,
        source_id: Any = _UNSET,
        author_id: Any = _UNSET,
        source_valid_from: Any = _UNSET,
        source_valid_to: Any = _UNSET,
        visibility: Any = _UNSET,
        new_id: str | None = None,
        valid_from: datetime | None = None,
    ) -> dict[str, Any]:
        """Create a new Fact version and close the previous interval.

        The previous version gets ``valid_to = now`` and ``status = obsolete``;
        the new node is linked with ``(old)-[:VERSION_OF]->(new)``. A Version
        audit node records the update. No node is ever deleted.
        """
        if confidence is not _UNSET:
            self._validate_confidence(confidence)
        if status is not _UNSET:
            self._validate_status(status)
        now = _now()

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            logical_id = self._resolve_logical_id(tx, fact_id)
            if logical_id is None:
                raise NotFoundError(f"Fact {fact_id!r} not found")
            current = tx.run(
                """
                MATCH (f:Fact {logical_id: $logical_id})
                WHERE f.valid_to IS NULL
                RETURN f
                ORDER BY f.valid_from DESC
                LIMIT 1
                """,
                logical_id=logical_id,
            ).single()
            if current is None:
                raise NotFoundError(f"Fact {fact_id!r} has no current version")
            old_props = self._node_dict(current["f"])
            old_id = old_props["id"]

            entity_row = tx.run(
                """
                MATCH (e:Entity)-[:HAS_FACT]->(old:Fact {id: $old_id})
                RETURN e.id AS entity_id
                """,
                old_id=old_id,
            ).single()
            if entity_row is None:
                raise NotFoundError(f"Fact {old_id!r} is not linked to an Entity")

            new_props = dict(old_props)
            new_props.pop("id", None)
            new_props.pop("valid_to", None)
            new_props["logical_id"] = logical_id
            new_props["valid_from"] = valid_from or now
            new_props["status"] = "valid" if status is _UNSET else status
            if value is not _UNSET:
                new_props["value"] = value
            if property is not _UNSET:
                new_props["property"] = property
            if confidence is not _UNSET:
                new_props["confidence"] = confidence
            if source_id is not _UNSET:
                if source_id is None:
                    new_props.pop("source_id", None)
                else:
                    new_props["source_id"] = source_id
            if author_id is not _UNSET:
                if author_id is None:
                    new_props.pop("author_id", None)
                else:
                    new_props["author_id"] = author_id
            if source_valid_from is not _UNSET:
                if source_valid_from is None:
                    new_props.pop("source_valid_from", None)
                else:
                    new_props["source_valid_from"] = source_valid_from
            if source_valid_to is not _UNSET:
                if source_valid_to is None:
                    new_props.pop("source_valid_to", None)
                else:
                    new_props["source_valid_to"] = source_valid_to
            if visibility is not _UNSET:
                new_props = apply_visibility(new_props, visibility)

            new_id_value = new_id or f"{logical_id}__v_{uuid4().hex[:12]}"
            version_props: dict[str, Any] = {
                "created_at": now,
                "change_type": "update",
            }
            if author_id is not _UNSET and author_id is not None:
                version_props["author_id"] = author_id

            tx.run(
                """
                MATCH (old:Fact {id: $old_id})
                SET old.valid_to = $now, old.status = 'obsolete'
                """,
                old_id=old_id,
                now=now,
            )
            record = tx.run(
                """
                MATCH (old:Fact {id: $old_id})
                MATCH (e:Entity {id: $entity_id})
                CREATE (new:Fact {id: $new_id})
                SET new += $props
                CREATE (old)-[:VERSION_OF]->(new)
                CREATE (e)-[:HAS_FACT]->(new)
                CREATE (v:Version {id: $version_id})
                SET v += $version_props
                CREATE (v)-[:VERSIONS]->(new)
                RETURN new
                """,
                old_id=old_id,
                entity_id=entity_row["entity_id"],
                new_id=new_id_value,
                props=new_props,
                version_id=f"{logical_id}__audit_{uuid4().hex[:12]}",
                version_props=version_props,
            ).single()
            return self._node_dict(record["new"])

        return self._write(work)

    def invalidate_fact(
        self,
        fact_id: str,
        *,
        author_id: str | None = None,
        valid_to: datetime | None = None,
    ) -> dict[str, Any]:
        """Close the current Fact interval without creating a new version."""
        now = _now()

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            logical_id = self._resolve_logical_id(tx, fact_id)
            if logical_id is None:
                raise NotFoundError(f"Fact {fact_id!r} not found")
            current = tx.run(
                """
                MATCH (f:Fact {logical_id: $logical_id})
                WHERE f.valid_to IS NULL
                RETURN f
                ORDER BY f.valid_from DESC
                LIMIT 1
                """,
                logical_id=logical_id,
            ).single()
            if current is None:
                raise NotFoundError(f"Fact {fact_id!r} has no current version")
            node_id = current["f"]["id"]
            version_props: dict[str, Any] = {
                "created_at": now,
                "change_type": "invalidate",
            }
            if author_id is not None:
                version_props["author_id"] = author_id
            record = tx.run(
                """
                MATCH (f:Fact {id: $node_id})
                SET f.valid_to = $valid_to, f.status = 'obsolete'
                CREATE (v:Version {id: $version_id})
                SET v += $version_props
                CREATE (v)-[:VERSIONS]->(f)
                RETURN f
                """,
                node_id=node_id,
                valid_to=valid_to or now,
                version_id=f"{logical_id}__audit_{uuid4().hex[:12]}",
                version_props=version_props,
            ).single()
            return self._node_dict(record["f"])

        return self._write(work)

    def get_facts_for_entity(
        self, entity_id: str, *, include_obsolete: bool = False
    ) -> list[dict[str, Any]]:
        """Return current Facts of an Entity (optionally including obsolete)."""

        def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
            result = tx.run(
                """
                MATCH (e:Entity {id: $entity_id})-[:HAS_FACT]->(f:Fact)
                WHERE $include_obsolete OR f.valid_to IS NULL
                RETURN f
                ORDER BY f.property
                """,
                entity_id=entity_id,
                include_obsolete=include_obsolete,
            )
            return [self._node_dict(record["f"]) for record in result]

        return self._read(work)

    # ------------------------------------------------------------- RELATES_TO
    def create_relation(
        self,
        *,
        source_entity_id: str,
        target_entity_id: str,
        relation: str,
        confidence: str = "EXTRACTED",
        status: str = "valid",
        source_id: str | None = None,
        source_valid_from: datetime | None = None,
        source_valid_to: datetime | None = None,
        valid_from: datetime | None = None,
    ) -> dict[str, Any]:
        """Create or update a current RELATES_TO arc between two Entities."""
        self._validate_confidence(confidence)
        self._validate_status(status)
        props = _without_none(
            {
                "confidence": confidence,
                "status": status,
                "valid_from": valid_from or _now(),
                "source_id": source_id,
                "source_valid_from": source_valid_from,
                "source_valid_to": source_valid_to,
            }
        )

        def work(tx: ManagedTransaction) -> dict[str, Any]:
            for entity_id in (source_entity_id, target_entity_id):
                found = tx.run(
                    "MATCH (e:Entity {id: $id}) RETURN e", id=entity_id
                ).single()
                if found is None:
                    raise NotFoundError(f"Entity {entity_id!r} not found")
            record = tx.run(
                """
                MATCH (a:Entity {id: $source})
                MATCH (b:Entity {id: $target})
                MERGE (a)-[r:RELATES_TO {relation: $relation}]->(b)
                SET r += $props, r.valid_to = null
                RETURN r, a.id AS source_id, b.id AS target_id
                """,
                source=source_entity_id,
                target=target_entity_id,
                relation=relation,
                props=props,
            ).single()
            return self._rel_dict(
                record["r"], record["source_id"], record["target_id"]
            )

        return self._write(work)

    def get_relations(
        self, entity_id: str | None = None, *, include_obsolete: bool = False
    ) -> list[dict[str, Any]]:
        """Return current RELATES_TO arcs, optionally filtered by source Entity."""

        def work(tx: ManagedTransaction) -> list[dict[str, Any]]:
            if entity_id is None:
                result = tx.run(
                    """
                    MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
                    WHERE $include_obsolete OR r.valid_to IS NULL
                    RETURN r, a.id AS source_id, b.id AS target_id
                    ORDER BY r.relation
                    """,
                    include_obsolete=include_obsolete,
                )
            else:
                result = tx.run(
                    """
                    MATCH (a:Entity {id: $entity_id})-[r:RELATES_TO]->(b:Entity)
                    WHERE $include_obsolete OR r.valid_to IS NULL
                    RETURN r, a.id AS source_id, b.id AS target_id
                    ORDER BY r.relation
                    """,
                    entity_id=entity_id,
                    include_obsolete=include_obsolete,
                )
            return [
                self._rel_dict(record["r"], record["source_id"], record["target_id"])
                for record in result
            ]

        return self._read(work)

    def get_relation(
        self, source_entity_id: str, target_entity_id: str, relation: str
    ) -> dict[str, Any] | None:
        """Return a current RELATES_TO arc, or None when missing."""

        def work(tx: ManagedTransaction) -> dict[str, Any] | None:
            record = tx.run(
                """
                MATCH (a:Entity {id: $source})-[r:RELATES_TO {relation: $relation}]->(b:Entity {id: $target})
                WHERE r.valid_to IS NULL
                RETURN r, a.id AS source_id, b.id AS target_id
                """,
                source=source_entity_id,
                target=target_entity_id,
                relation=relation,
            ).single()
            if record is None:
                return None
            return self._rel_dict(
                record["r"], record["source_id"], record["target_id"]
            )

        return self._read(work)
