"""Conflict API tests (WP6, Gate G6)."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.conflict import scan_conflicts
from app.storage.repository import GraphRepository

from .conftest import auth_header, login

PREFIX = "g67_"


def _setup_conflict(repo: GraphRepository, pg_conn) -> dict:
    repo.create_entity(entity_id=f"{PREFIX}api_entity", label="Entity")
    repo.create_fact(
        fact_id=f"{PREFIX}api_fact_a",
        entity_id=f"{PREFIX}api_entity",
        property="state",
        value="a",
        source_id=f"{PREFIX}api_src_a",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}api_fact_b",
        entity_id=f"{PREFIX}api_entity",
        property="state",
        value="b",
        source_id=f"{PREFIX}api_src_b",
    )
    created = scan_conflicts(repo, pg_conn)
    assert len(created) == 1
    return created[0]


def test_list_conflicts_requires_auth(client: TestClient) -> None:
    response = client.get("/api/v1/conflicts")
    assert response.status_code == 401


def test_list_conflicts_as_viewer(
    client: TestClient, viewer_user, repo: GraphRepository, pg_conn
) -> None:
    _setup_conflict(repo, pg_conn)
    token = login(client, viewer_user["username"])
    response = client.get("/api/v1/conflicts", headers=auth_header(token))
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert any(c["entity_id"] == f"{PREFIX}api_entity" for c in data)


def test_approve_requires_admin_or_editor(
    client: TestClient, viewer_user, repo: GraphRepository, pg_conn
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    token = login(client, viewer_user["username"])
    response = client.post(
        f"/api/v1/conflicts/{conflict['id']}/approve",
        json={"choice": "a"},
        headers=auth_header(token),
    )
    assert response.status_code == 403


def test_approve_as_admin(
    client: TestClient, admin_user, repo: GraphRepository, pg_conn
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    token = login(client, admin_user["username"])
    response = client.post(
        f"/api/v1/conflicts/{conflict['id']}/approve",
        json={"choice": "a"},
        headers=auth_header(token),
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "approved"
    assert repo.get_fact(f"{PREFIX}api_fact_b") is None


def test_reject_as_editor(
    client: TestClient, editor_user, repo: GraphRepository, pg_conn
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    token = login(client, editor_user["username"])
    response = client.post(
        f"/api/v1/conflicts/{conflict['id']}/reject",
        headers=auth_header(token),
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"


def test_approve_invalid_choice_returns_422(
    client: TestClient, admin_user, repo: GraphRepository, pg_conn
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    token = login(client, admin_user["username"])
    response = client.post(
        f"/api/v1/conflicts/{conflict['id']}/approve",
        json={"choice": "c"},
        headers=auth_header(token),
    )
    assert response.status_code == 422
