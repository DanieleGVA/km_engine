"""Test end-to-end della Web UI di adjudication (WP-E6, GE6)."""
from __future__ import annotations

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.auth.users import create_user
from app.conflict import scan_conflicts
from app.domain.verify import create_adjudication, create_glossary_proposal
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility

TEST_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get("KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"),
)

PREFIX = "ie6_"
PASSWORD = "ie6-test-password-123"


def cleanup_pg(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM conflicts WHERE entity_id LIKE %s", (f"{PREFIX}%",))
        conn.execute("DELETE FROM adjudications WHERE document_id LIKE %s", (f"{PREFIX}%",))
        conn.execute(
            "DELETE FROM glossary_proposals WHERE term LIKE %s OR context LIKE %s",
            (f"{PREFIX}%", f"{PREFIX}%"),
        )
        rows = conn.execute(
            "SELECT id FROM users WHERE username LIKE %s", (f"{PREFIX}%",)
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (ids, [str(i) for i in ids]),
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM teams WHERE name LIKE %s", (f"{PREFIX}%",))


def cleanup_neo4j(client: Neo4jClient) -> None:
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PREFIX,
        )
        session.run(
            "MATCH (s:Source) WHERE s.uri STARTS WITH $prefix DETACH DELETE s",
            prefix=PREFIX,
        )


@pytest.fixture
def pg_conn():
    conn = psycopg.connect(TEST_DSN, autocommit=True)
    cleanup_pg(conn)
    yield conn
    cleanup_pg(conn)
    conn.close()


@pytest.fixture
def neo4j_client() -> Neo4jClient:
    c = Neo4jClient.from_env()
    c.verify_connectivity()
    cleanup_neo4j(c)
    yield c
    cleanup_neo4j(c)
    c.close()


@pytest.fixture
def repo(neo4j_client: Neo4jClient) -> GraphRepository:
    return GraphRepository(neo4j_client)


@pytest.fixture
def admin_user(pg_conn):
    return create_user(
        pg_conn,
        f"{PREFIX}admin",
        f"{PREFIX}admin@example.test",
        PASSWORD,
        roles=("admin",),
    )


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Il rate limiter e' un singleton di processo: reset per test pulito."""
    from app.api.app import _rate_limiter

    _rate_limiter._buckets.clear()
    yield


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app=app, base_url="http://test") as c:
        yield c


def _login(client: TestClient, username: str) -> str:
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_ui_adjudication_flow_end_to_end(
    client: TestClient, pg_conn, admin_user, repo: GraphRepository
) -> None:
    token = _login(client, admin_user["username"])
    headers = _auth(token)

    # Setup: una code L3, una proposta glossario e un conflitto pending.
    create_adjudication(pg_conn, f"{PREFIX}doc", "steps", "divergence", suggestion="fix")
    create_glossary_proposal(pg_conn, f"{PREFIX}term", context=f"{PREFIX}doc")

    repo.create_entity(
        entity_id=f"{PREFIX}entity",
        label="ConflictEntity",
        type="code",
        visibility=Visibility(is_public=True),
    )
    repo.create_fact(
        fact_id=f"{PREFIX}fact_a",
        entity_id=f"{PREFIX}entity",
        property="status",
        value="active",
        source_id=f"{PREFIX}source_a",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}fact_b",
        entity_id=f"{PREFIX}entity",
        property="status",
        value="inactive",
        source_id=f"{PREFIX}source_b",
    )
    with neo4j_session(repo.client) as session:
        for source_id in (f"{PREFIX}source_a", f"{PREFIX}source_b"):
            session.run(
                """
                MERGE (s:Source {id: $id})
                SET s.uri = $uri, s.type = 'file', s.hash = $hash,
                    s.language = 'en', s.ingested_at = datetime()
                """,
                id=source_id,
                uri=f"{PREFIX}uri_{source_id}",
                hash=f"{PREFIX}hash_{source_id}",
            )
    created = scan_conflicts(repo, pg_conn)
    assert created, "expected a pending conflict"

    # GET /ui/ mostra le tre code.
    page = client.get("/ui/", headers=headers)
    assert page.status_code == 200
    assert "Adjudication UI" in page.text
    assert f"{PREFIX}doc" in page.text
    assert f"{PREFIX}term" in page.text
    assert f"{PREFIX}entity" in page.text

    # Approve L3 e proposta; approve conflitto scegliendo A.
    adjudication_id = pg_conn.execute(
        "SELECT id FROM adjudications WHERE document_id = %s", (f"{PREFIX}doc",)
    ).fetchone()[0]
    proposal_id = pg_conn.execute(
        "SELECT id FROM glossary_proposals WHERE term = %s", (f"{PREFIX}term",)
    ).fetchone()[0]
    conflict_id = created[0]["id"]

    r = client.post(f"/ui/adjudications/{adjudication_id}/approve", headers=headers)
    assert r.status_code in (200, 303)
    r = client.post(f"/ui/glossary-proposals/{proposal_id}/approve", headers=headers)
    assert r.status_code in (200, 303)
    r = client.post(
        f"/ui/conflicts/{conflict_id}/approve", headers=headers, data={"choice": "a"}
    )
    assert r.status_code in (200, 303)

    # Verifica stato su Postgres e grafo.
    row = pg_conn.execute(
        "SELECT status FROM adjudications WHERE id = %s", (adjudication_id,)
    ).fetchone()
    assert row[0] == "approved"
    row = pg_conn.execute(
        "SELECT status FROM glossary_proposals WHERE id = %s", (proposal_id,)
    ).fetchone()
    assert row[0] == "approved"
    row = pg_conn.execute(
        "SELECT status FROM conflicts WHERE id = %s", (conflict_id,)
    ).fetchone()
    assert row[0] == "approved"

    # Il fatto perdente (B) deve essere obsolete; A resta valido.
    fact_b = repo.get_fact(f"{PREFIX}fact_b")
    assert fact_b is None or fact_b.get("status") == "obsolete"


def test_ui_requires_admin_or_editor(client: TestClient, pg_conn) -> None:
    viewer = create_user(
        pg_conn,
        f"{PREFIX}viewer",
        f"{PREFIX}viewer@example.test",
        PASSWORD,
        roles=("viewer",),
    )
    token = _login(client, viewer["username"])
    response = client.get("/ui/", headers=_auth(token))
    assert response.status_code == 403


def neo4j_session(client: Neo4jClient):
    return client.session()
