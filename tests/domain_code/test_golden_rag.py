"""GD3: golden-set retrieval on the code domain (>=50 queries, Recall@5 >= 0.85).

Reuses ``app.rag`` unchanged: ``build_embedding_from_graph``,
``populate_embeddings`` and ``rag_query`` are the same functions used by the
recipe domain. The golden set is generated deterministically from the corpus
symbols (functions/classes/modules) with natural-language templates.
"""
from __future__ import annotations

from app.auth import Principal
from app.rag.rag import (
    build_embedding_from_graph,
    populate_embeddings,
    rag_query,
)
from app.storage.client import Neo4jClient
from code_domain.mapping import map_graphify_to_graph
from scripts.load_domain_pack import load_pack


def _admin_principal() -> Principal:
    return Principal(
        "id_code_u_admin", ("admin",), (), "default", "id_code_j_admin"
    )


def test_code_golden_recall_at_5(
    client: Neo4jClient, graph, pack, modules, golden
) -> None:
    assert len(golden) >= 50

    load_pack(client, "domain-packs/code")
    map_graphify_to_graph(client, graph, pack, doc_prefix="id_code_")

    embedding = build_embedding_from_graph(client, pack)
    populated = populate_embeddings(client, embedding)
    assert populated == len(modules)

    principal = _admin_principal()
    hits_at_5 = 0
    for pair in golden:
        hits = rag_query(
            client,
            principal,
            pair["query"],
            lang="en",
            limit=5,
            embedding=embedding,
        )
        returned = [hit.document_id for hit in hits]
        if pair["document_id"] in returned:
            hits_at_5 += 1

    recall = hits_at_5 / len(golden)
    assert recall >= 0.85, f"Recall@5 {recall:.4f} below 0.85"
