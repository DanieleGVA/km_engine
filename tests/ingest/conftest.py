"""Shared fixtures for WP4 ingestion tests (clean ``wp4_`` data)."""

from __future__ import annotations

import os

import psycopg
import pytest

from app.ingest.config import IngestSettings
from app.ingest.jobs import JobManager
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

TEST_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get(
        "KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"
    ),
)

NEO4J_PREFIX = "wp4_"


def cleanup_postgres(conn: psycopg.Connection) -> None:
    """Delete only WP4 test jobs (source_uri prefixed ``wp4_``)."""
    with conn.transaction():
        conn.execute("DELETE FROM ingest_jobs WHERE source_uri LIKE 'wp4\\_%'")


def cleanup_neo4j(client: Neo4jClient) -> None:
    """Delete only WP4 test nodes (id prefixed ``wp4_``)."""
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


@pytest.fixture()
def conn():
    c = psycopg.connect(TEST_DSN, autocommit=True)
    cleanup_postgres(c)
    try:
        yield c
    finally:
        cleanup_postgres(c)
        c.close()


@pytest.fixture()
def client():
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    cleanup_neo4j(neo4j_client)
    try:
        yield neo4j_client
    finally:
        cleanup_neo4j(neo4j_client)
        neo4j_client.close()


@pytest.fixture()
def repo(client: Neo4jClient) -> GraphRepository:
    return GraphRepository(client)


@pytest.fixture()
def settings(tmp_path) -> IngestSettings:
    return IngestSettings(
        pg_dsn=TEST_DSN,
        chunk_size=2,
        cache_dir=tmp_path / "km_ingest_cache",
    )


@pytest.fixture()
def jobs(conn, settings) -> JobManager:
    return JobManager(conn, settings.cache_dir / "jobs")
