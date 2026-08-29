"""WP8 — Gate G9: fixtures della suite E2E (dati con prefisso ``e2e_``).

La suite gira sullo stack dev reale (container ``km-neo4j`` + ``km-postgres``)
con l'app FastAPI vera (``create_app``). Tutti i dati creati dal flusso hanno
prefisso ``e2e_`` e vengono rimossi in teardown (Postgres + Neo4j).
"""
from __future__ import annotations

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.auth.config import AuthSettings
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

TEST_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    ),
)

PREFIX = "e2e_"
ADMIN_USERNAME = f"{PREFIX}admin"
ADMIN_PASSWORD = "e2e-admin-password-123"  # >= 12 char (ADR-002 D5)
VIEWER_PASSWORD = "e2e-viewer-password-123"


def cleanup_postgres(conn: psycopg.Connection) -> None:
    """Delete only the rows created by the E2E flow (``e2e_`` prefixed)."""
    with conn.transaction():
        rows = conn.execute(
            "SELECT id FROM users WHERE username LIKE %s", (f"{PREFIX}%",)
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (ids, [str(i) for i in ids]),
            )
            conn.execute(
                "DELETE FROM refresh_tokens WHERE user_id = ANY(%s)", (ids,)
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
        conn.execute(
            "DELETE FROM conflicts WHERE entity_id LIKE %s", (f"{PREFIX}%",)
        )
        conn.execute(
            "DELETE FROM audit_log WHERE entity_id LIKE %s", (f"{PREFIX}%",)
        )
        # NOTA: pattern 'e2e%' (non 'e2e\\_%'): i job hanno source_uri 'e2e://...'
        conn.execute(
            "DELETE FROM ingest_jobs WHERE source_uri LIKE %s", (f"{PREFIX}%",)
        )
        conn.execute("DELETE FROM teams WHERE name LIKE %s", (f"{PREFIX}%",))


def cleanup_neo4j(client: Neo4jClient) -> None:
    """Delete only the nodes created by the E2E flow (``e2e_`` prefixed)."""
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


@pytest.fixture(scope="module")
def settings() -> AuthSettings:
    """Impostazioni auth E2E: admin dedicato ``e2e_admin``."""
    return AuthSettings(
        pg_dsn=TEST_DSN,
        jwt_secret="e2e-test-jwt-secret-0123456789abcdef",  # >= 32 bytes (RFC 7518 3.2)
        admin_username=ADMIN_USERNAME,
        admin_password=ADMIN_PASSWORD,
    )


@pytest.fixture(scope="module")
def conn(settings: AuthSettings):
    """Connessione Postgres per setup/verifica/cleanup del flusso."""
    c = psycopg.connect(settings.pg_dsn, autocommit=True)
    cleanup_postgres(c)
    try:
        yield c
    finally:
        cleanup_postgres(c)
        c.close()


@pytest.fixture(scope="module")
def neo4j_client() -> Neo4jClient:
    """Client Neo4j per setup/verifica/cleanup del flusso."""
    c = Neo4jClient.from_env()
    c.verify_connectivity()
    cleanup_neo4j(c)
    try:
        yield c
    finally:
        cleanup_neo4j(c)
        c.close()


@pytest.fixture(scope="module")
def repo(neo4j_client: Neo4jClient) -> GraphRepository:
    return GraphRepository(neo4j_client)


@pytest.fixture(scope="module")
def app():
    """Applicazione FastAPI reale (stessa usata da uvicorn in dev)."""
    return create_app()


@pytest.fixture(scope="module")
def client(app):
    """TestClient HTTP sull'app reale (stack dev: Neo4j + Postgres reali)."""
    with TestClient(app=app, base_url="http://test") as c:
        yield c


def login(client: TestClient, username: str, password: str) -> str:
    """Login via API reale (POST /auth/login) e ritorna l'access token."""
    response = client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
