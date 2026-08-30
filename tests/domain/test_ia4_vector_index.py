"""WP-A4 — Indice vettoriale su Document.embedding (384-dim, cosine).

Verifica empirica su Neo4j 5.26.30 Community: l'indice vettoriale è supportato
e ONLINE. Il test è ATTIVO (nessun fallback finto): controlla l'indice in
SHOW INDEXES e ne verifica il funzionamento con una query vettoriale reale.
"""
from __future__ import annotations

from app.storage.client import Neo4jClient

TEST_PREFIX = "ia4_"
VECTOR_INDEX = "document_embedding_vector"
DIMENSIONS = 384


def _unit_vector(dim: int = DIMENSIONS) -> list[float]:
    """Vettore unitario a ``dim`` dimensioni (prima componente = 1.0)."""
    return [1.0] + [0.0] * (dim - 1)


def test_vector_index_exists_and_online(client: Neo4jClient) -> None:
    """L'indice vettoriale esiste, è VECTOR e ONLINE."""
    with client.session() as session:
        record = session.run(
            """
            SHOW INDEXES YIELD name, type, labelsOrTypes, properties, state
            WHERE name = $name
            RETURN name, type, labelsOrTypes, properties, state
            """,
            name=VECTOR_INDEX,
        ).single()

    assert record is not None, f"indice vettoriale {VECTOR_INDEX} non trovato"
    assert record["type"] == "VECTOR"
    assert record["labelsOrTypes"] == ["Document"]
    assert record["properties"] == ["embedding"]
    assert record["state"] == "ONLINE"


def test_vector_query_returns_document(client: Neo4jClient) -> None:
    """Una query vettoriale reale restituisce il documento atteso."""
    doc_id = f"{TEST_PREFIX}doc_vector"
    embedding = _unit_vector()

    with client.session() as session:
        session.run(
            """
            CREATE (d:Document {
                id: $id,
                title: 'Vector document',
                lang: 'en',
                source_lang: 'it',
                canonical_hash: 'hash-vector',
                verification_level: 'L1',
                translation_state: 'native',
                source_language: 'it',
                embedding: $embedding
            })
            """,
            id=doc_id,
            embedding=embedding,
        )

        record = session.run(
            """
            CALL db.index.vector.queryNodes($index, 3, $vector)
            YIELD node, score
            RETURN node.id AS id, score
            """,
            index=VECTOR_INDEX,
            vector=embedding,
        ).single()

    assert record is not None
    assert record["id"] == doc_id
    assert float(record["score"]) == 1.0
