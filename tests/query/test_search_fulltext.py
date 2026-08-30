"""Test search full-text Neo4j (WP-E2, GE2): stessi risultati + nuovi match."""
from __future__ import annotations

import pytest

from app.auth import Principal
from app.query.engine import search
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility

PREFIX = "ie2_"
VIEWER = Principal(f"{PREFIX}u_viewer", ("viewer",), (), "default", f"{PREFIX}j_viewer")


def cleanup(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Fact OR n:Source OR n:Document OR n:CanonicalTerm)
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


def _await_ft(client: Neo4jClient) -> None:
    with client.session() as session:
        for index in (
            "entity_label_fulltext",
            "entity_type_fulltext",
            "fact_value_fulltext",
            "fact_property_fulltext",
            "document_title_fulltext",
            "canonical_term_label_en_fulltext",
        ):
            session.run("CALL db.awaitIndex($index, 5)", index=index)


def test_search_matches_entity_fact_document_term(
    client: Neo4jClient, repo: GraphRepository
) -> None:
    repo.create_entity(
        entity_id=f"{PREFIX}entity",
        label="MyFunction",
        type="code",
        visibility=Visibility(is_public=True),
    )
    repo.create_fact(
        fact_id=f"{PREFIX}fact",
        entity_id=f"{PREFIX}entity",
        property="name",
        value="uniquevalue",
        source_id=f"{PREFIX}source",
    )
    with client.session() as session:
        session.run(
            """
            MERGE (d:Document {id: $id})
            SET d.title = 'Fulltext Document', d.lang = 'en', d.source_lang = 'it',
                d.canonical_hash = 'hash', d.verification_level = 'L1',
                d.translation_state = 'translated', d.source_language = 'it',
                d.is_public = true, d.roles = [], d.teams = []
            """,
            id=f"{PREFIX}doc",
        )
        session.run(
            """
            MERGE (t:CanonicalTerm {id: $id})
            SET t.namespace = 'ie2_test', t.term_id = $id, t.label_en = 'Fulltext Term',
                t.label_it = 'Fulltext Term', t.is_public = true, t.roles = [], t.teams = []
            """,
            id=f"{PREFIX}term",
        )
    _await_ft(client)

    entity_hits = search(client, VIEWER, "MyFunc", await_indexes=True)
    assert any(h["id"] == f"{PREFIX}entity" and h["match_type"] == "entity" for h in entity_hits)

    fact_hits = search(client, VIEWER, "uniquevalue", await_indexes=True)
    assert any(h["id"] == f"{PREFIX}fact" and h["match_type"] == "fact" for h in fact_hits)

    doc_hits = search(client, VIEWER, "Fulltext", await_indexes=True)
    assert any(h["id"] == f"{PREFIX}doc" and h["match_type"] == "document" for h in doc_hits)
    assert any(h["id"] == f"{PREFIX}term" and h["match_type"] == "term" for h in doc_hits)


def test_search_fulltext_respects_visibility(
    client: Neo4jClient, repo: GraphRepository
) -> None:
    repo.create_entity(
        entity_id=f"{PREFIX}public_entity",
        label="PublicClass",
        type="code",
        visibility=Visibility(is_public=True),
    )
    repo.create_entity(
        entity_id=f"{PREFIX}private_entity",
        label="PrivateClass",
        type="code",
        visibility=Visibility(roles=("admin",)),
    )
    _await_ft(client)

    hits = search(client, VIEWER, "Class", await_indexes=True)
    assert any(h["id"] == f"{PREFIX}public_entity" for h in hits)
    assert not any(h["id"] == f"{PREFIX}private_entity" for h in hits)
