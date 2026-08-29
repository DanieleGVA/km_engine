"""Shared fixtures for storage tests against the dev Neo4j container."""

from __future__ import annotations

import pytest

from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

TEST_PREFIX = "wp2test_"


def clean_test_data(client: Neo4jClient) -> None:
    """Delete all nodes created by the storage test suite."""
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Entity OR n:Fact OR n:Source OR n:Version)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=TEST_PREFIX,
        )
        session.run(
            """
            MATCH (s:Source)
            WHERE s.uri STARTS WITH $prefix
            DETACH DELETE s
            """,
            prefix=TEST_PREFIX,
        )


@pytest.fixture
def client() -> Neo4jClient:
    """Provide a connected client and clean test data before/after."""
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    clean_test_data(neo4j_client)
    yield neo4j_client
    clean_test_data(neo4j_client)
    neo4j_client.close()


@pytest.fixture
def repo(client: Neo4jClient) -> GraphRepository:
    """Provide a repository bound to the test client."""
    return GraphRepository(client)
