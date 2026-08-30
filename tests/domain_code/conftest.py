"""Shared fixtures for Iteration D code-domain tests.

Data prefix: ``id_code_``. Cleanup removes only ``id_code_`` nodes. The code
corpus is a committed copy of ``app/domain/*.py`` under
``tests/fixtures/corpus_code``; graphify extraction is session-scoped because
it is the slowest step.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.domain import load_domain_pack
from app.storage.client import Neo4jClient

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "domain-packs" / "code"
DRAFT_DIR = REPO_ROOT / "domain-packs" / "code-agents-draft"
CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_code"

PREFIX = "id_code_"


def cleanup_neo4j(client: Neo4jClient) -> None:
    """Delete only the nodes created by code-domain tests (``id_code_``)."""
    with client.session() as session:
        session.run(
            """
            MATCH (n)
            WHERE (n:Document OR n:CanonicalTerm OR n:DomainPack OR n:Entity
                   OR n:Fact OR n:Source)
              AND n.id STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PREFIX,
        )


@pytest.fixture()
def client() -> Neo4jClient:
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    cleanup_neo4j(neo4j_client)
    try:
        yield neo4j_client
    finally:
        cleanup_neo4j(neo4j_client)
        neo4j_client.close()


@pytest.fixture(scope="session")
def pack():
    return load_domain_pack(PACK_DIR)


@pytest.fixture(scope="session")
def graph(tmp_path_factory):
    from code_domain.mapping import graphify_extract_corpus

    cache_root = tmp_path_factory.mktemp("graphify_code_cache")
    return graphify_extract_corpus(CORPUS_DIR, cache_root=cache_root)


@pytest.fixture(scope="session")
def modules(graph):
    from code_domain.mapping import collect_modules

    return collect_modules(graph)


@pytest.fixture(scope="session")
def golden(modules):
    from code_domain.golden import build_golden_set

    return build_golden_set(modules)
