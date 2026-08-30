"""Code-domain RAG orchestration (Iteration D, WP-D3).

Reuses ``app.rag`` unchanged: ``build_embedding_from_graph``,
``populate_embeddings`` and ``rag_query`` are the same functions used by the
recipe domain. This module only wires them together for the code pack/corpus.
"""
from __future__ import annotations

from typing import Any

from app.auth import Principal
from app.domain.pack import DomainPackBundle
from app.rag.rag import (
    build_embedding_from_graph,
    populate_embeddings,
    rag_query,
)
from app.storage.client import Neo4jClient


def build_code_embedding(
    client: Neo4jClient,
    pack: DomainPackBundle,
) -> Any:
    """Build the deterministic embedding vocabulary from the code pack + graph."""
    return build_embedding_from_graph(client, pack)


def populate_code_embeddings(client: Neo4jClient, embedding: Any) -> int:
    """Populate ``Document.embedding`` for code documents (idempotent)."""
    return populate_embeddings(client, embedding)


def query_code(
    client: Neo4jClient,
    principal: Principal,
    query: str,
    *,
    embedding: Any | None = None,
    limit: int = 5,
) -> list[Any]:
    """Run a natural-language query over the code domain graph."""
    return rag_query(
        client,
        principal,
        query,
        lang="en",
        limit=limit,
        embedding=embedding,
    )
