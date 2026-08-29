"""Invalidation API tests (WP6, Gate G7)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

from .conftest import create_source, link_derived_from

PREFIX = "g67_"


def _login(client: TestClient, username: str) -> str:
    response = client.post(
        "/auth/login",
        json={"username": username, "password": "g67-test-password-123"},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_invalidate_source_requires_admin_or_editor(
    client: TestClient, viewer_user, repo: GraphRepository, neo4j_client: Neo4jClient
) -> None:
    create_source(neo4j_client, f"{PREFIX}api_src")
    token = _login(client, viewer_user["username"])
    response = client.post(
        f"/api/v1/sources/{PREFIX}api_src/invalidate",
        json={"reason": "test"},
        headers=_auth(token),
    )
    assert response.status_code == 403


def test_invalidate_source_as_admin(
    client: TestClient, admin_user, repo: GraphRepository, neo4j_client: Neo4jClient
) -> None:
    repo.create_entity(entity_id=f"{PREFIX}api_entity", label="Entity")
    create_source(neo4j_client, f"{PREFIX}api_src")
    repo.create_fact(
        fact_id=f"{PREFIX}api_fact",
        entity_id=f"{PREFIX}api_entity",
        property="state",
        value="a",
        source_id=f"{PREFIX}api_src",
    )
    link_derived_from(neo4j_client, f"{PREFIX}api_fact", f"{PREFIX}api_src")

    token = _login(client, admin_user["username"])
    response = client.post(
        f"/api/v1/sources/{PREFIX}api_src/invalidate",
        json={"reason": "source changed"},
        headers=_auth(token),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["invalidated_facts"] == [f"{PREFIX}api_fact"]
    assert repo.get_fact(f"{PREFIX}api_fact") is None
