"""API tests for the RAG/document/glossary endpoints (WP-B1/B2/B4)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.auth.users import create_user
from app.rag.rag import build_embedding_from_graph, populate_embeddings
from app.storage.client import Neo4jClient
from tests.rag.conftest import (
    PREFIX,
    create_canonical_term,
    create_document,
    link_entity_to_document,
)

TEST_PASSWORD = "ib-api-password-123"


@pytest.fixture()
def api_app():
    return create_app()


@pytest.fixture()
def api_client(api_app):
    with TestClient(app=api_app, base_url="http://test") as test_client:
        yield test_client


@pytest.fixture()
def api_user(pg_conn):
    user = create_user(
        pg_conn,
        f"{PREFIX}api_user",
        f"{PREFIX}api@example.test",
        TEST_PASSWORD,
        roles=("viewer",),
    )
    return {
        "username": f"{PREFIX}api_user",
        "password": TEST_PASSWORD,
        "user_id": str(user["id"]),
    }


def _auth_headers(api_client: TestClient, user: dict) -> dict[str, str]:
    response = api_client.post(
        "/auth/login",
        json={"username": user["username"], "password": user["password"]},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _setup_documents(client: Neo4jClient) -> None:
    create_document(
        client,
        "ib_api_public",
        title="public tomato soup",
        source_title="Zuppa di pomodoro pubblica",
        is_public=True,
    )
    create_document(
        client,
        "ib_api_hidden",
        title="secret tomato sauce",
        source_title="Sugo segreto",
    )
    embedding = build_embedding_from_graph(client)
    populate_embeddings(client, embedding)


def test_ib_api_rag_query_visibility(
    client: Neo4jClient, api_client: TestClient, api_user: dict
) -> None:
    _setup_documents(client)
    headers = _auth_headers(api_client, api_user)

    response = api_client.post(
        "/api/v1/rag/query",
        json={"query": "tomato", "lang": "it", "limit": 5},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    hits = response.json()
    assert isinstance(hits, list)
    assert hits, "expected at least one visible hit"
    ids = {hit["document_id"] for hit in hits}
    assert "ib_api_public" in ids
    assert "ib_api_hidden" not in ids
    assert all("canonical_md" in hit for hit in hits)
    assert all("match_reason" in hit for hit in hits)


def test_ib_api_get_document_and_visibility(
    client: Neo4jClient, api_client: TestClient, api_user: dict
) -> None:
    _setup_documents(client)
    headers = _auth_headers(api_client, api_user)

    public = api_client.get("/api/v1/documents/ib_api_public", headers=headers)
    assert public.status_code == 200, public.text
    body = public.json()
    assert body["document_id"] == "ib_api_public"
    assert "canonical_md" in body
    assert body["canonical_md"].startswith("---")

    hidden = api_client.get("/api/v1/documents/ib_api_hidden", headers=headers)
    assert hidden.status_code == 404


def test_ib_api_glossary_query(
    client: Neo4jClient, api_client: TestClient, api_user: dict
) -> None:
    create_canonical_term(
        client,
        "ib_api_term_garlic",
        namespace="ingredienti",
        label_en="garlic",
        label_it="aglio",
        is_public=True,
    )
    create_document(
        client,
        "ib_api_doc_garlic",
        title="garlic pasta",
        source_title="Pasta all'aglio",
        is_public=True,
    )
    link_entity_to_document(
        client,
        "ib_api_ent_garlic",
        "ib_api_doc_garlic",
        label="garlic",
        entity_type="ingredient",
        term_id="ib_api_term_garlic",
    )

    headers = _auth_headers(api_client, api_user)
    response = api_client.get(
        "/api/v1/glossary/query", params={"ingredient": "garlic"}, headers=headers
    )
    assert response.status_code == 200, response.text
    results = response.json()
    assert [item["document_id"] for item in results] == ["ib_api_doc_garlic"]
    assert results[0]["term"]["namespace"] == "ingredienti"


def test_ib_api_glossary_query_requires_one_selector(
    api_client: TestClient, api_user: dict
) -> None:
    headers = _auth_headers(api_client, api_user)
    response = api_client.get("/api/v1/glossary/query", headers=headers)
    assert response.status_code == 422
