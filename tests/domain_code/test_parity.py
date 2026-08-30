"""GD1 parity: code pack mapping vs the legacy graphify path.

The comparison is on function/class names and dependency relation triples
(never UUIDs). The reference is the graphify build output itself; the code
pack mapping must reproduce exactly the same symbol labels and the same
``RELATES_TO`` triples between function/class entities.
"""
from __future__ import annotations

from app.storage.client import Neo4jClient
from code_domain.mapping import (
    map_graphify_to_graph,
    reference_labels_and_triples,
)
from scripts.load_domain_pack import load_pack


def _actual_labels_and_triples(
    client: Neo4jClient,
) -> tuple[set[str], set[tuple[str, str, str]]]:
    labels: set[str] = set()
    triples: set[tuple[str, str, str]] = set()
    with client.session() as session:
        for record in session.run(
            """
            MATCH (e:Entity)
            WHERE e.id STARTS WITH 'id_code_' AND e.type IN ['function', 'class']
            RETURN e.label AS label
            """
        ):
            labels.add(str(record["label"]))
        for record in session.run(
            """
            MATCH (a:Entity)-[r:RELATES_TO]->(b:Entity)
            WHERE a.id STARTS WITH 'id_code_' AND b.id STARTS WITH 'id_code_'
            RETURN a.label AS src, r.relation AS rel, b.label AS tgt
            """
        ):
            triples.add(
                (str(record["src"]), str(record["rel"]), str(record["tgt"]))
            )
    return labels, triples


def test_code_pack_parity_with_graphify(
    client: Neo4jClient, graph, pack
) -> None:
    expected_labels, expected_triples = reference_labels_and_triples(graph)

    load_pack(client, "domain-packs/code")
    result = map_graphify_to_graph(client, graph, pack, doc_prefix="id_code_")

    actual_labels, actual_triples = _actual_labels_and_triples(client)

    assert result.functions + result.classes >= len(expected_labels)
    assert actual_labels == expected_labels
    assert actual_triples == expected_triples
    assert len(actual_triples) == result.relations
