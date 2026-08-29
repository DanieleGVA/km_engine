"""Shared fixtures for WP6 invalidation tests (clean ``g67_`` data)."""
from __future__ import annotations

import os
from datetime import UTC, datetime

import psycopg
import pytest

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
    return create_user(
        pg_conn,
        f"{PREFIX}resolver",
        f"{PREFIX}resolver@example.test",
        TEST_PASSWORD,
        roles=("admin",),
    )


@pytest.fixture
def app():
    from app.api.app import create_app

    return create_app()


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

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


def link_derived_from(
    client: Neo4jClient, fact_id: str, source_id: str
) -> None:
    """Link a Fact to a Source through DERIVED_FROM (provenance)."""
    with client.session() as session:
        session.run(
            """
            MATCH (f:Fact {id: $fact_id})
            MATCH (s:Source {id: $source_id})
            MERGE (f)-[:DERIVED_FROM]->(s)
            """,
            fact_id=fact_id,
            source_id=source_id,
        )


def link_fact_to_fact(
    client: Neo4jClient, dependent_fact_id: str, parent_fact_id: str
) -> None:
    """Link a dependent Fact to a parent Fact through DERIVED_FROM."""
    with client.session() as session:
        session.run(
            """
            MATCH (d:Fact {id: $dependent_id})
            MATCH (p:Fact {id: $parent_id})
            MERGE (d)-[:DERIVED_FROM]->(p)
            """,
            dependent_id=dependent_fact_id,
            parent_id=parent_fact_id,
        )
