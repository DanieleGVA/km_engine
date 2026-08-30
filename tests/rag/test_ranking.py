"""Unit tests for deterministic RAG ranking (WP-B1, gate GB1)."""
from __future__ import annotations

from app.domain.embedding import DeterministicEmbedding
from app.rag.rag import (
    LANG_BOOST,
    VERIFICATION_BOOST,
    _lang_boost,
    _verification_boost,
    populate_embeddings,
    rag_query,
)
from app.storage.client import Neo4jClient
from tests.rag.conftest import create_document


def test_ib_lang_boost_rules() -> None:
    doc = {"source_lang": "it", "source_language": "it", "lang": "en"}
    assert _lang_boost(doc, None) == 0.0
    assert _lang_boost(doc, "it") == LANG_BOOST
    assert _lang_boost(doc, "en") == LANG_BOOST
    assert _lang_boost(doc, "fr") == 0.0


def test_ib_verification_boost_is_strictly_increasing() -> None:
    assert (
        _verification_boost({"verification_level": "L1"})
        < _verification_boost({"verification_level": "L2"})
        < _verification_boost({"verification_level": "L3"})
    )
    assert VERIFICATION_BOOST["L1"] < VERIFICATION_BOOST["L2"] < VERIFICATION_BOOST["L3"]


def test_ib_score_formula_is_explainable() -> None:
    cosine = 0.8
    lang_boost = _lang_boost({"source_lang": "it", "lang": "en"}, "it")
    verification_boost = _verification_boost({"verification_level": "L2"})
    final = cosine * (1.0 + lang_boost) * (1.0 + verification_boost)
    assert final == 0.8 * (1.0 + LANG_BOOST) * (1.0 + VERIFICATION_BOOST["L2"])


def test_ib_rag_query_stable_order_and_reason(
    client: Neo4jClient, principal_admin
) -> None:
    """Ranking is deterministic and match_reason exposes the score parts."""
    embedding = DeterministicEmbedding.from_texts(
        ["spaghetti tomato garlic", "chocolate cake sugar"]
    )
    create_document(
        client,
        "ib_doc_spaghetti",
        title="spaghetti tomato garlic",
        source_title="Spaghetti al pomodoro",
        embedding=embedding.embed("spaghetti tomato garlic"),
    )
    create_document(
        client,
        "ib_doc_cake",
        title="chocolate cake sugar",
        source_title="Torta al cioccolato",
        embedding=embedding.embed("chocolate cake sugar"),
    )

    first = rag_query(
        client,
        principal_admin,
        "spaghetti tomato garlic",
        lang="it",
        limit=5,
        embedding=embedding,
    )
    second = rag_query(
        client,
        principal_admin,
        "spaghetti tomato garlic",
        lang="it",
        limit=5,
        embedding=embedding,
    )

    assert [hit.document_id for hit in first] == [hit.document_id for hit in second]
    assert first[0].document_id == "ib_doc_spaghetti"
    assert first[0].score > first[1].score
    assert "cosine=" in first[0].match_reason
    assert "boost_lang=" in first[0].match_reason
    assert "boost_verification=" in first[0].match_reason
    assert "final=" in first[0].match_reason


def test_ib_populate_embeddings_is_idempotent(
    client: Neo4jClient, principal_admin
) -> None:
    """populate_embeddings fills only missing embeddings and is idempotent."""
    embedding = DeterministicEmbedding.from_texts(["spaghetti garlic"])
    create_document(client, "ib_doc_pop", title="spaghetti garlic")

    first = populate_embeddings(client, embedding)
    second = populate_embeddings(client, embedding)
    assert first == 1
    assert second == 0

    with client.session() as session:
        record = session.run(
            "MATCH (d:Document {id: $id}) RETURN d.embedding AS embedding",
            id="ib_doc_pop",
        ).single()
    assert record is not None
    assert len(record["embedding"]) == 384
