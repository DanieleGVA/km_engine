"""Test rollback pack: storico bitemporale intatto (WP-E4, GE4)."""
from __future__ import annotations

import pytest

from app.ops.rollback import apply_rollback_versions, snapshot_document_facts
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility

PREFIX = "ie4_"


def cleanup(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PREFIX,
        )


@pytest.fixture
def client() -> Neo4jClient:
    c = Neo4jClient.from_env()
    c.verify_connectivity()
    cleanup(c)
    yield c
    cleanup(c)
    c.close()


@pytest.fixture
def repo(client: Neo4jClient) -> GraphRepository:
    return GraphRepository(client)


def _create_document(client: Neo4jClient, doc_id: str) -> None:
    with client.session() as session:
        session.run(
            """
            MERGE (d:Document {id: $id})
            SET d.title = 'rollback doc', d.lang = 'en', d.source_lang = 'it',
                d.canonical_hash = 'hash', d.verification_level = 'L1',
                d.translation_state = 'translated', d.source_language = 'it',
                d.is_public = true, d.roles = [], d.teams = []
            """,
            id=doc_id,
        )


def test_rollback_versions_preserve_bitemporal_history(
    client: Neo4jClient, repo: GraphRepository
) -> None:
    doc_id = f"{PREFIX}doc"
    entity_id = f"{PREFIX}entity"
    fact_id = f"{PREFIX}fact"

    _create_document(client, doc_id)
    repo.create_entity(
        entity_id=entity_id,
        label="ingredient",
        type="ingredient",
        visibility=Visibility(is_public=True),
    )
    with client.session() as session:
        session.run(
            """
            MATCH (d:Document {id: $doc_id})
            MATCH (e:Entity {id: $entity_id})
            MERGE (e)-[:PART_OF_DOC]->(d)
            """,
            doc_id=doc_id,
            entity_id=entity_id,
        )
    repo.create_fact(
        fact_id=fact_id,
        entity_id=entity_id,
        property="qty",
        value="200",
        source_id=f"{PREFIX}source",
    )

    snapshot = snapshot_document_facts(client, doc_id)
    assert fact_id in snapshot
    assert snapshot[fact_id]["value"] == "200"

    # Simula la ri-estrazione con il pack vN-1: il valore cambia in place.
    with client.session() as session:
        session.run(
            """
            MATCH (f:Fact {logical_id: $logical_id})
            WHERE f.valid_to IS NULL
            SET f.value = '180'
            """,
            logical_id=fact_id,
        )

    changes = apply_rollback_versions(client, doc_id, snapshot)
    assert changes == [{"logical_id": fact_id, "action": "version"}]

    history = repo.get_fact_history(fact_id)
    assert len(history) == 2
    values = {h["value"] for h in history}
    assert values == {"200", "180"}

    # La versione corrente e' quella vN-1; la vN e' obsolete e collegata.
    current = repo.get_fact(fact_id)
    assert current is not None
    assert current["value"] == "180"
    assert current["status"] == "valid"

    with client.session() as session:
        record = session.run(
            """
            MATCH (old:Fact {logical_id: $logical_id})-[:VERSION_OF]->(new:Fact)
            WHERE old.status = 'obsolete' AND new.valid_to IS NULL
            RETURN old.value AS old_value, new.value AS new_value
            """,
            logical_id=fact_id,
        ).single()
    assert record is not None
    assert record["old_value"] == "200"
    assert record["new_value"] == "180"


def test_rollback_invalidates_disappeared_fact(
    client: Neo4jClient, repo: GraphRepository
) -> None:
    doc_id = f"{PREFIX}doc2"
    entity_id = f"{PREFIX}entity2"
    fact_id = f"{PREFIX}fact2"

    _create_document(client, doc_id)
    repo.create_entity(
        entity_id=entity_id,
        label="ingredient",
        type="ingredient",
        visibility=Visibility(is_public=True),
    )
    with client.session() as session:
        session.run(
            """
            MATCH (d:Document {id: $doc_id})
            MATCH (e:Entity {id: $entity_id})
            MERGE (e)-[:PART_OF_DOC]->(d)
            """,
            doc_id=doc_id,
            entity_id=entity_id,
        )
    repo.create_fact(
        fact_id=fact_id,
        entity_id=entity_id,
        property="unit",
        value="g",
        source_id=f"{PREFIX}source2",
    )

    snapshot = snapshot_document_facts(client, doc_id)
    # Simula la scomparsa del fatto dopo ri-estrazione: rimuove il legame
    # HAS_FACT corrente, cosi' il fatto non e' piu' tra i current del doc.
    with client.session() as session:
        session.run(
            """
            MATCH (e:Entity {id: $entity_id})-[r:HAS_FACT]->(f:Fact {logical_id: $logical_id})
            DELETE r
            """,
            entity_id=entity_id,
            logical_id=fact_id,
        )

    changes = apply_rollback_versions(client, doc_id, snapshot)
    assert changes == [{"logical_id": fact_id, "action": "invalidate"}]

    with client.session() as session:
        record = session.run(
            "MATCH (f:Fact {logical_id: $logical_id}) RETURN f.status AS status, f.valid_to AS valid_to",
            logical_id=fact_id,
        ).single()
    assert record is not None
    assert record["status"] == "obsolete"
    assert record["valid_to"] is not None
