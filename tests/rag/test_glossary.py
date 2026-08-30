"""Integration: structured glossary queries (WP-B2, gate GB2)."""
from __future__ import annotations

from app.rag.rag import glossary_query
from app.storage.client import Neo4jClient
from tests.rag.conftest import (
    create_canonical_term,
    create_document,
    link_entity_to_document,
)


def _setup_graph(client: Neo4jClient) -> None:
    create_canonical_term(
        client,
        "ib_term_tech",
        namespace="tecnica",
        label_en="soffritto",
        label_it="soffritto",
        is_public=True,
    )
    create_canonical_term(
        client,
        "ib_term_ing",
        namespace="ingredienti",
        label_en="garlic",
        label_it="aglio",
        is_public=True,
    )
    create_canonical_term(
        client,
        "ib_term_state",
        namespace="stati",
        label_en="golden",
        label_it="dorato",
        is_public=True,
    )

    create_document(client, "ib_doc_a", title="spaghetti garlic", is_public=True)
    create_document(client, "ib_doc_b", title="golden potatoes", teams=["ib_team_a"])

    link_entity_to_document(
        client,
        "ib_ent_a_tech",
        "ib_doc_a",
        label="soffritto",
        entity_type="technique",
        term_id="ib_term_tech",
    )
    link_entity_to_document(
        client,
        "ib_ent_a_ing",
        "ib_doc_a",
        label="garlic",
        entity_type="ingredient",
        term_id="ib_term_ing",
    )
    link_entity_to_document(
        client,
        "ib_ent_b_state",
        "ib_doc_b",
        label="golden",
        entity_type="state",
        term_id="ib_term_state",
    )


def test_ib_glossary_query_by_term_id(client: Neo4jClient, principal_admin) -> None:
    _setup_graph(client)
    results = glossary_query(client, principal_admin, term_id="ib_term_tech")
    assert [item["document_id"] for item in results] == ["ib_doc_a"]
    assert results[0]["term"]["namespace"] == "tecnica"
    assert results[0]["entities"][0]["type"] == "technique"


def test_ib_glossary_query_by_technique_label(
    client: Neo4jClient, principal_admin
) -> None:
    _setup_graph(client)
    results = glossary_query(client, principal_admin, technique="soffritto")
    assert [item["document_id"] for item in results] == ["ib_doc_a"]


def test_ib_glossary_query_by_ingredient_and_state(
    client: Neo4jClient, principal_admin
) -> None:
    _setup_graph(client)
    ingredient_results = glossary_query(client, principal_admin, ingredient="garlic")
    state_results = glossary_query(client, principal_admin, state="golden")

    assert [item["document_id"] for item in ingredient_results] == ["ib_doc_a"]
    assert [item["document_id"] for item in state_results] == ["ib_doc_b"]


def test_ib_glossary_query_visibility(
    client: Neo4jClient, principal_team_a, principal_team_b, principal_admin
) -> None:
    _setup_graph(client)
    # doc_b is team_a only; a team_b viewer must not see it through the state path.
    team_b_results = glossary_query(client, principal_team_b, state="golden")
    team_a_results = glossary_query(client, principal_team_a, state="golden")
    admin_results = glossary_query(client, principal_admin, state="golden")

    assert team_b_results == []
    assert [item["document_id"] for item in team_a_results] == ["ib_doc_b"]
    assert [item["document_id"] for item in admin_results] == ["ib_doc_b"]


def test_ib_glossary_query_requires_selector(client: Neo4jClient, principal_admin) -> None:
    assert glossary_query(client, principal_admin) == []
