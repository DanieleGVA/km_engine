"""Gate G3 — shared fixtures (Postgres auth + Neo4j storage, clean g3_ data).

All rows/nodes created by these tests carry the ``g3_`` prefix and are removed
after each test. The dev containers (docker-compose.yml) host the real schema;
tests never touch the schema itself.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from app.auth import create_user
from app.auth.config import AuthSettings
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository
from tests.integration.constants import TEST_PASSWORD

TEST_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    ),
)

NEO4J_PREFIX = "g3_"


def cleanup_postgres(conn: psycopg.Connection) -> None:
    """Delete only the rows created by gate G3 tests (g3_ prefixed)."""
    with conn.transaction():
        rows = conn.execute(
            "SELECT id FROM users WHERE username LIKE 'g3\\_%'"
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (ids, [str(i) for i in ids]),
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM teams WHERE name LIKE 'g3\\_%'")


def cleanup_neo4j(client: Neo4jClient) -> None:
    """Delete only the nodes created by gate G3 tests (g3_ prefixed)."""
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=NEO4J_PREFIX,
        )
        session.run(
            "MATCH (s:Source) WHERE s.uri STARTS WITH $prefix DETACH DELETE s",
            prefix=NEO4J_PREFIX,
        )


@pytest.fixture()
def settings() -> AuthSettings:
    return AuthSettings(
        pg_dsn=TEST_DSN,
        jwt_secret="g3-test-jwt-secret-0123456789abcdef",  # >= 32 bytes (RFC 7518 3.2)
        admin_username="g3_bootstrap_admin",
        admin_password="g3-test-admin-password-123",
    )


@pytest.fixture()
def conn(settings: AuthSettings):
    c = psycopg.connect(settings.pg_dsn, autocommit=True)
    try:
        yield c
    finally:
        cleanup_postgres(c)
        c.close()


@pytest.fixture()
def client() -> Neo4jClient:
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    cleanup_neo4j(neo4j_client)
    yield neo4j_client
    cleanup_neo4j(neo4j_client)
    neo4j_client.close()


@pytest.fixture()
def repo(client: Neo4jClient) -> GraphRepository:
    return GraphRepository(client)


@pytest.fixture()
def make_g3_user(conn: psycopg.Connection):
    """Factory: create a g3_ user with roles/teams; return create_user's dict."""

    def _make(
        suffix: str,
        *,
        roles: tuple[str, ...] = ("viewer",),
        teams: tuple[str, ...] = (),
        password: str = TEST_PASSWORD,
    ) -> dict:
        return create_user(
            conn,
            f"g3_{suffix}",
            f"g3_{suffix}@example.test",
            password,
            roles=roles,
            teams=teams,
        )

    return _make
