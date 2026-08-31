"""Fixture per i test di validazione ricette (branch validate-recipe)."""
from __future__ import annotations

import pathlib

import pytest

from app.storage.client import Neo4jClient
from tests.rag.conftest import extract_committed_pack

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session")
def pack_dir(tmp_path_factory) -> pathlib.Path:
    return extract_committed_pack(tmp_path_factory.mktemp("pack"))


@pytest.fixture()
def client() -> Neo4jClient:
    neo4j_client = Neo4jClient.from_env()
    neo4j_client.verify_connectivity()
    yield neo4j_client
    neo4j_client.close()
