"""Shared fixtures for WP6 conflict tests (clean ``g67_`` data)."""
from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.app import create_app
from app.auth.db import connect as pg_connect
from app.auth.users import create_user
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

TEST_PG_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    ),
)

PREFIX = "g67_"
TEST_PASSWORD = "g67-test-password-123"


def cleanup_neo4j(client: Neo4jClient) -> None:
    """Delete only the nodes created by WP6 conflict tests (g67_ prefixed)."""
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


def cleanup_postgres(conn: psycopg.Connection) -> None:
    """Delete only the rows created by WP6 conflict tests (g67_ prefixed)."""
    with conn.transaction():
        conn.execute(
            "DELETE FROM conflicts WHERE entity_id LIKE %s", (f"{PREFIX}%",)
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
def pg_conn():
    conn = pg_connect()
    cleanup_postgres(conn)
    yield conn
    cleanup_postgres(conn)
    conn.close()


@pytest.fixture
def g67_user(pg_conn):
    """A real Postgres user used as resolver in workflow tests."""
    return create_user(
        pg_conn,
        f"{PREFIX}resolver",
        f"{PREFIX}resolver@example.test",
        TEST_PASSWORD,
        roles=("admin",),
    )


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def client(app):
    with TestClient(app=app, base_url="http://test") as c:
        yield c


@pytest.fixture
def admin_user(pg_conn):
    return create_user(
        pg_conn,
        f"{PREFIX}admin",
        f"{PREFIX}admin@example.test",
        TEST_PASSWORD,
        roles=("admin",),
    )


@pytest.fixture
def editor_user(pg_conn):
    return create_user(
        pg_conn,
        f"{PREFIX}editor",
        f"{PREFIX}editor@example.test",
        TEST_PASSWORD,
        roles=("editor",),
    )


@pytest.fixture
def viewer_user(pg_conn):
    return create_user(
        pg_conn,
        f"{PREFIX}viewer",
        f"{PREFIX}viewer@example.test",
        TEST_PASSWORD,
        roles=("viewer",),
    )


def create_source(
    client: Neo4jClient,
    source_id: str,
    *,
    uri: str | None = None,
    ingested_at: datetime | None = None,
) -> None:
    """Create/refresh a Source node for detection suggestion tests."""
    with client.session() as session:
        session.run(
            """
            MERGE (s:Source {id: $id})
            SET s.uri = $uri,
                s.type = 'file',
                s.hash = $hash,
                s.language = 'en',
                s.ingested_at = $ingested_at
            """,
            id=source_id,
            uri=uri or f"{PREFIX}uri_{source_id}",
            hash=f"{PREFIX}hash_{source_id}",
            ingested_at=ingested_at or datetime.now(UTC),
        )


def list_g67_conflicts(conn, status=None):
    """List only the conflicts created by WP6 tests (g67_ entity prefix)."""
    from app.conflict import list_conflicts

    return [
        c for c in list_conflicts(conn, status=status)
        if c["entity_id"].startswith(PREFIX)
    ]


def login(client: TestClient, username: str) -> str:
    response = client.post(
        "/auth/login",
        json={"username": username, "password": TEST_PASSWORD},
    )
    assert response.status_code == 200
    return response.json()["access_token"]


def auth_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
