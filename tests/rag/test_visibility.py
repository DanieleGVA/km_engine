"""Integration: visibility filter in vector retrieval (WP-B1, gate GB1)."""
from __future__ import annotations

from app.domain.embedding import DeterministicEmbedding
from app.rag.rag import rag_query
from app.storage.client import Neo4jClient
from tests.rag.conftest import create_document


def test_ib_vector_retrieval_never_leaks_hidden_documents(
    client: Neo4jClient, principal_team_a, principal_team_b, principal_admin
) -> None:
    """Two users, same query: zero non-visible documents, even vector matches."""
    embedding = DeterministicEmbedding.from_texts(
        ["secret tomato sauce", "public tomato soup"]
    )
    create_document(
        client,
        "ib_doc_public",
        title="public tomato soup",
        source_title="Zuppa di pomodoro pubblica",
        is_public=True,
        embedding=embedding.embed("public tomato soup"),
    )
    create_document(
        client,
        "ib_doc_secret",
        title="secret tomato sauce",
        source_title="Sugo segreto",
        teams=["ib_team_a"],
        embedding=embedding.embed("secret tomato sauce"),
    )

    query = "tomato"

    team_a_hits = rag_query(
        client, principal_team_a, query, limit=5, embedding=embedding
    )
    team_b_hits = rag_query(
        client, principal_team_b, query, limit=5, embedding=embedding
    )
    admin_hits = rag_query(
        client, principal_admin, query, limit=5, embedding=embedding
    )

    team_a_ids = {hit.document_id for hit in team_a_hits}
    team_b_ids = {hit.document_id for hit in team_b_hits}
    admin_ids = {hit.document_id for hit in admin_hits}

    # Team A sees the public document and its own team document.
    assert "ib_doc_public" in team_a_ids
    assert "ib_doc_secret" in team_a_ids

    # Team B sees only the public document: the vector match on the secret
    # document is filtered before ranking/return.
    assert "ib_doc_public" in team_b_ids
    assert "ib_doc_secret" not in team_b_ids

    # Admin bypass sees both.
    assert admin_ids == {"ib_doc_public", "ib_doc_secret"}


def test_ib_vector_retrieval_default_deny(
    client: Neo4jClient, principal_viewer, principal_admin
) -> None:
    """A default-deny document is never returned to a viewer."""
    embedding = DeterministicEmbedding.from_texts(["hidden garlic recipe"])
    create_document(
        client,
        "ib_doc_hidden",
        title="hidden garlic recipe",
        source_title="Ricetta segreta all'aglio",
        embedding=embedding.embed("hidden garlic recipe"),
    )

    viewer_hits = rag_query(
        client, principal_viewer, "hidden garlic recipe", limit=5, embedding=embedding
    )
    admin_hits = rag_query(
        client, principal_admin, "hidden garlic recipe", limit=5, embedding=embedding
    )

    assert all(hit.document_id != "ib_doc_hidden" for hit in viewer_hits)
    assert any(hit.document_id == "ib_doc_hidden" for hit in admin_hits)
