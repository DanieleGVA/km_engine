"""Bitemporal versioning tests (ADR-001 D3)."""

from __future__ import annotations

import pytest

from app.storage.client import Neo4jClient
from app.storage.errors import NotFoundError
from app.storage.repository import GraphRepository


def _create_fact(repo: GraphRepository, fact_id: str = "wp2test_ver_fact_1") -> None:
    repo.create_entity(entity_id="wp2test_ver_entity_1", label="module")
    repo.create_fact(
        fact_id=fact_id,
        entity_id="wp2test_ver_entity_1",
        property="exports",
        value="v1",
        confidence="EXTRACTED",
    )


def test_update_creates_new_version_and_closes_old(repo: GraphRepository) -> None:
    _create_fact(repo)
    old = repo.get_fact("wp2test_ver_fact_1")
    assert old is not None

    new = repo.update_fact("wp2test_ver_fact_1", value="v2", author_id="editor-1")
    assert new["id"] != old["id"]
    assert new["logical_id"] == "wp2test_ver_fact_1"
    assert new["value"] == "v2"
    assert new["valid_to"] is None
    assert new["status"] == "valid"

    current = repo.get_fact("wp2test_ver_fact_1")
    assert current is not None
    assert current["id"] == new["id"]
    assert current["value"] == "v2"

    history = repo.get_fact_history("wp2test_ver_fact_1")
    assert len(history) == 2
    assert history[0]["id"] == old["id"]
    assert history[0]["value"] == "v1"
    assert history[0]["valid_to"] is not None
    assert history[0]["status"] == "obsolete"
    assert history[1]["id"] == new["id"]


def test_get_fact_by_old_version_id_returns_current(repo: GraphRepository) -> None:
    _create_fact(repo)
    old = repo.get_fact("wp2test_ver_fact_1")
    new = repo.update_fact("wp2test_ver_fact_1", value="v2")
    fetched = repo.get_fact(old["id"])
    assert fetched is not None
    assert fetched["id"] == new["id"]


def test_update_preserves_entity_link(repo: GraphRepository) -> None:
    _create_fact(repo)
    repo.update_fact("wp2test_ver_fact_1", value="v2")
    facts = repo.get_facts_for_entity("wp2test_ver_entity_1")
    assert len(facts) == 1
    assert facts[0]["value"] == "v2"
    all_facts = repo.get_facts_for_entity(
        "wp2test_ver_entity_1", include_obsolete=True
    )
    assert len(all_facts) == 2


def test_version_of_chain_exists(repo: GraphRepository, client: Neo4jClient) -> None:
    _create_fact(repo)
    old = repo.get_fact("wp2test_ver_fact_1")
    new = repo.update_fact("wp2test_ver_fact_1", value="v2")
    with client.session() as session:
        record = session.run(
            """
            MATCH (old:Fact {id: $old_id})-[r:VERSION_OF]->(new:Fact {id: $new_id})
            RETURN count(r) AS count
            """,
            old_id=old["id"],
            new_id=new["id"],
        ).single()
    assert record is not None
    assert record["count"] == 1


def test_invalidate_closes_interval_without_new_version(
    repo: GraphRepository, client: Neo4jClient
) -> None:
    _create_fact(repo)
    invalidated = repo.invalidate_fact(
        "wp2test_ver_fact_1", author_id="editor-1"
    )
    assert invalidated["valid_to"] is not None
    assert invalidated["status"] == "obsolete"

    assert repo.get_fact("wp2test_ver_fact_1") is None
    history = repo.get_fact_history("wp2test_ver_fact_1")
    assert len(history) == 1
    assert history[0]["status"] == "obsolete"

    with client.session() as session:
        count = session.run(
            "MATCH (f:Fact {logical_id: $id}) RETURN count(f) AS count",
            id="wp2test_ver_fact_1",
        ).single()["count"]
    assert count == 1


def test_update_and_invalidate_missing_raise(repo: GraphRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.update_fact("wp2test_ver_fact_missing", value="v")
    with pytest.raises(NotFoundError):
        repo.invalidate_fact("wp2test_ver_fact_missing")


def test_audit_version_nodes_are_created(
    repo: GraphRepository, client: Neo4jClient
) -> None:
    _create_fact(repo)
    repo.update_fact("wp2test_ver_fact_1", value="v2", author_id="editor-1")
    repo.invalidate_fact("wp2test_ver_fact_1", author_id="editor-1")
    with client.session() as session:
        records = session.run(
            """
            MATCH (v:Version)-[:VERSIONS]->(f:Fact {logical_id: $id})
            RETURN v.change_type AS change_type, v.author_id AS author_id
            ORDER BY v.created_at
            """,
            id="wp2test_ver_fact_1",
        ).data()
    assert [r["change_type"] for r in records] == ["update", "invalidate"]
    assert all(r["author_id"] == "editor-1" for r in records)
