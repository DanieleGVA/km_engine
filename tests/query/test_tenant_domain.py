"""Test isolamento multi-tenant su Document/CanonicalTerm (WP-E5, GE5)."""
from __future__ import annotations

import pytest

from app.auth import Principal
from app.query.domain import (
    list_canonical_terms,
    list_documents,
    search_canonical_terms,
    search_documents,
)
from app.storage.client import Neo4jClient

PREFIX = "ie5_"


def cleanup(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:CanonicalTerm)
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


def _create_document(
    client: Neo4jClient, doc_id: str, title: str, tenant: str | None
) -> None:
    with client.session() as session:
        session.run(
            """
            MERGE (d:Document {id: $id})
            SET d.title = $title,
                d.lang = 'en',
                d.source_lang = 'it',
                d.canonical_hash = $hash,
                d.verification_level = 'L1',
                d.translation_state = 'translated',
                d.source_language = 'it',
                d.is_public = true,
                d.roles = [],
                d.teams = [],
                d.tenant = $tenant
            """,
            id=doc_id,
            title=title,
            hash=f"hash-{doc_id}",
            tenant=tenant,
        )


def _create_term(
    client: Neo4jClient, term_id: str, label: str, tenant: str | None
) -> None:
    with client.session() as session:
        session.run(
            """
            MERGE (t:CanonicalTerm {id: $id})
            SET t.namespace = 'ie5_test',
                t.term_id = $term_id,
                t.label_en = $label,
                t.label_it = $label,
                t.is_public = true,
                t.roles = [],
                t.teams = [],
                t.tenant = $tenant
            """,
            id=term_id,
            term_id=term_id,
            label=label,
            tenant=tenant,
        )


def _principal(tenant: str) -> Principal:
    return Principal(f"{PREFIX}u_{tenant}", ("viewer",), (), tenant, f"{PREFIX}j_{tenant}")


def _await_ft(client: Neo4jClient) -> None:
    with client.session() as session:
        for index in ("document_title_fulltext", "canonical_term_label_en_fulltext"):
            session.run("CALL db.awaitIndex($index, 5)", index=index)


def test_tenant_documents_are_isolated(client: Neo4jClient) -> None:
    _create_document(client, f"{PREFIX}doc_a", "Tenant A Recipe", "tenant_a")
    _create_document(client, f"{PREFIX}doc_b", "Tenant B Recipe", "tenant_b")

    docs_a = list_documents(client, _principal("tenant_a"))
    docs_b = list_documents(client, _principal("tenant_b"))
    docs_admin = list_documents(
        client, Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")
    )

    assert [d["id"] for d in docs_a] == [f"{PREFIX}doc_a"]
    assert [d["id"] for d in docs_b] == [f"{PREFIX}doc_b"]
    assert {d["id"] for d in docs_admin} == {f"{PREFIX}doc_a", f"{PREFIX}doc_b"}


def test_tenant_document_search_is_isolated(client: Neo4jClient) -> None:
    _create_document(client, f"{PREFIX}doc_a", "Shared Title", "tenant_a")
    _create_document(client, f"{PREFIX}doc_b", "Shared Title", "tenant_b")
    _await_ft(client)

    hits_a = search_documents(client, _principal("tenant_a"), "Shared")
    hits_b = search_documents(client, _principal("tenant_b"), "Shared")

    assert [h["id"] for h in hits_a] == [f"{PREFIX}doc_a"]
    assert [h["id"] for h in hits_b] == [f"{PREFIX}doc_b"]


def test_glossaries_shared_by_default_terms_can_be_scoped(
    client: Neo4jClient,
) -> None:
    # ADR-007: glossari condivisi (tenant=None) + termini privati opzionali.
    _create_term(client, f"{PREFIX}term_shared", "shared term", None)
    _create_term(client, f"{PREFIX}term_a", "tenant a term", "tenant_a")
    _create_term(client, f"{PREFIX}term_b", "tenant b term", "tenant_b")

    terms_a = list_canonical_terms(client, _principal("tenant_a"))
    terms_b = list_canonical_terms(client, _principal("tenant_b"))

    ids_a = {t["id"] for t in terms_a}
    ids_b = {t["id"] for t in terms_b}
    assert f"{PREFIX}term_shared" in ids_a
    assert f"{PREFIX}term_a" in ids_a
    assert f"{PREFIX}term_b" not in ids_a
    assert f"{PREFIX}term_shared" in ids_b
    assert f"{PREFIX}term_b" in ids_b
    assert f"{PREFIX}term_a" not in ids_b


def test_tenant_term_search_is_isolated(client: Neo4jClient) -> None:
    _create_term(client, f"{PREFIX}term_a", "private term", "tenant_a")
    _create_term(client, f"{PREFIX}term_b", "private term", "tenant_b")
    _await_ft(client)

    hits_a = search_canonical_terms(client, _principal("tenant_a"), "private")
    hits_b = search_canonical_terms(client, _principal("tenant_b"), "private")

    assert [h["id"] for h in hits_a] == [f"{PREFIX}term_a"]
    assert [h["id"] for h in hits_b] == [f"{PREFIX}term_b"]
