"""T8 — Visibilità Document/CanonicalTerm (WP-A4, P4).

Criterio: default-deny confermato; team corretto vede; public vede; admin vede
tutto; nessun percorso di lettura non filtrato (inclusi full-text e storico).
"""
from __future__ import annotations

from app.auth import Principal
from app.query.domain import (
    get_document,
    get_document_by_entity,
    list_canonical_terms,
    list_documents,
    search_canonical_terms,
    search_documents,
)
from app.storage.client import Neo4jClient
from tests.domain.conftest import create_canonical_term, create_document

TEST_PREFIX = "ia4_"


def _ids(items: list[dict]) -> set[str]:
    return {item["id"] for item in items}


class TestDocumentVisibility:
    def test_default_deny(self, client: Neo4jClient, principal_no_team: Principal) -> None:
        """Documento senza visibilità esplicita: default-deny."""
        doc_id = f"{TEST_PREFIX}doc_default_deny"
        create_document(client, doc_id, title="Default deny document")

        assert get_document(client, principal_no_team, doc_id) is None
        assert doc_id not in _ids(list_documents(client, principal_no_team))

    def test_team_visibility(self, client: Neo4jClient) -> None:
        """Documento ristretto a team: solo il team corretto vede."""
        doc_id = f"{TEST_PREFIX}doc_team"
        create_document(client, doc_id, title="Team document", teams=["ia4_team_a"])

        viewer = Principal("u1", ("viewer",), ("ia4_team_a",), "default", "j1")
        other = Principal("u2", ("viewer",), ("ia4_team_b",), "default", "j2")

        assert get_document(client, viewer, doc_id) is not None
        assert doc_id in _ids(list_documents(client, viewer))
        assert get_document(client, other, doc_id) is None
        assert doc_id not in _ids(list_documents(client, other))

    def test_public_visibility(self, client: Neo4jClient, principal_no_team: Principal) -> None:
        """Documento pubblico: visibile a qualsiasi viewer."""
        doc_id = f"{TEST_PREFIX}doc_public"
        create_document(client, doc_id, title="Public document", is_public=True)

        assert get_document(client, principal_no_team, doc_id) is not None
        assert doc_id in _ids(list_documents(client, principal_no_team))

    def test_admin_bypass(self, client: Neo4jClient, principal_admin: Principal) -> None:
        """Admin vede tutto, anche i documenti default-deny."""
        hidden = f"{TEST_PREFIX}doc_hidden"
        create_document(client, hidden, title="Hidden document")

        assert get_document(client, principal_admin, hidden) is not None
        assert hidden in _ids(list_documents(client, principal_admin))

    def test_get_document_by_entity_filters(self, client: Neo4jClient) -> None:
        """Il percorso Entity->Document filtra i documenti per visibilità."""
        entity_id = f"{TEST_PREFIX}entity_1"
        public_doc = f"{TEST_PREFIX}doc_public_by_entity"
        hidden_doc = f"{TEST_PREFIX}doc_hidden_by_entity"

        create_document(client, public_doc, title="Public by entity", is_public=True)
        create_document(client, hidden_doc, title="Hidden by entity")

        with client.session() as session:
            session.run(
                """
                CREATE (e:Entity {id: $entity_id, label: 'Ingredient', type: 'doc'})
                """,
                entity_id=entity_id,
            )
            session.run(
                """
                MATCH (e:Entity {id: $entity_id})
                MATCH (d:Document {id: $doc_id})
                CREATE (e)-[:PART_OF_DOC]->(d)
                """,
                entity_id=entity_id,
                doc_id=public_doc,
            )
            session.run(
                """
                MATCH (e:Entity {id: $entity_id})
                MATCH (d:Document {id: $doc_id})
                CREATE (e)-[:PART_OF_DOC]->(d)
                """,
                entity_id=entity_id,
                doc_id=hidden_doc,
            )

        viewer = Principal("u1", ("viewer",), (), "default", "j1")
        docs = get_document_by_entity(client, viewer, entity_id)
        assert public_doc in _ids(docs)
        assert hidden_doc not in _ids(docs)

    def test_fulltext_search_filters(self, client: Neo4jClient) -> None:
        """La ricerca full-text su Document.title non fa trapelare documenti nascosti."""
        public_doc = f"{TEST_PREFIX}doc_ft_public"
        hidden_doc = f"{TEST_PREFIX}doc_ft_hidden"
        create_document(client, public_doc, title="Unique tomato soup", is_public=True)
        create_document(client, hidden_doc, title="Unique tomato secret")

        viewer = Principal("u1", ("viewer",), (), "default", "j1")
        results = search_documents(client, viewer, "Unique tomato")

        assert public_doc in _ids(results)
        assert hidden_doc not in _ids(results)


class TestCanonicalTermVisibility:
    def test_default_deny(self, client: Neo4jClient, principal_no_team: Principal) -> None:
        """Termine senza visibilità esplicita: default-deny."""
        term_id = f"{TEST_PREFIX}term_default_deny"
        create_canonical_term(client, term_id, label_en="Default deny term")

        assert term_id not in _ids(list_canonical_terms(client, principal_no_team))

    def test_team_visibility(self, client: Neo4jClient) -> None:
        """Termine ristretto a team: solo il team corretto vede."""
        term_id = f"{TEST_PREFIX}term_team"
        create_canonical_term(client, term_id, label_en="Team term", teams=["ia4_team_a"])

        viewer = Principal("u1", ("viewer",), ("ia4_team_a",), "default", "j1")
        other = Principal("u2", ("viewer",), ("ia4_team_b",), "default", "j2")

        assert term_id in _ids(list_canonical_terms(client, viewer))
        assert term_id not in _ids(list_canonical_terms(client, other))

    def test_public_visibility(self, client: Neo4jClient, principal_no_team: Principal) -> None:
        """Termine pubblico: visibile a qualsiasi viewer."""
        term_id = f"{TEST_PREFIX}term_public"
        create_canonical_term(client, term_id, label_en="Public term", is_public=True)

        assert term_id in _ids(list_canonical_terms(client, principal_no_team))

    def test_admin_bypass(self, client: Neo4jClient, principal_admin: Principal) -> None:
        """Admin vede tutti i termini, anche default-deny."""
        term_id = f"{TEST_PREFIX}term_hidden"
        create_canonical_term(client, term_id, label_en="Hidden term")

        assert term_id in _ids(list_canonical_terms(client, principal_admin))

    def test_fulltext_search_filters(self, client: Neo4jClient) -> None:
        """La ricerca full-text su CanonicalTerm.label_en non fa trapelare termini nascosti."""
        public_term = f"{TEST_PREFIX}term_ft_public"
        hidden_term = f"{TEST_PREFIX}term_ft_hidden"
        create_canonical_term(client, public_term, label_en="Blanching public", is_public=True)
        create_canonical_term(client, hidden_term, label_en="Blanching secret")

        viewer = Principal("u1", ("viewer",), (), "default", "j1")
        results = search_canonical_terms(client, viewer, "Blanching")

        assert public_term in _ids(results)
        assert hidden_term not in _ids(results)
