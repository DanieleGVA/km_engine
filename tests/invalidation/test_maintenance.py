"""Truth-maintenance tests (WP6, Gate G7)."""
from __future__ import annotations

import pytest

from app.invalidation import InvalidationError, SourceNotFoundError, invalidate_source
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

from .conftest import create_source, link_derived_from, link_fact_to_fact

PREFIX = "g67_"


def test_invalidate_source_marks_derived_facts_obsolete(
    repo: GraphRepository, pg_conn, neo4j_client: Neo4jClient, g67_user
) -> None:
    repo.create_entity(entity_id=f"{PREFIX}entity", label="Entity")
    create_source(neo4j_client, f"{PREFIX}src")
    repo.create_fact(
        fact_id=f"{PREFIX}f1",
        entity_id=f"{PREFIX}entity",
        property="state",
        value="a",
        source_id=f"{PREFIX}src",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}f2",
        entity_id=f"{PREFIX}entity",
        property="owner",
        value="b",
        source_id=f"{PREFIX}src",
    )
    link_derived_from(neo4j_client, f"{PREFIX}f1", f"{PREFIX}src")
    link_derived_from(neo4j_client, f"{PREFIX}f2", f"{PREFIX}src")

    result = invalidate_source(
        repo,
        pg_conn,
        f"{PREFIX}src",
        reason="source changed",
        user_id=str(g67_user["id"]),
    )

    assert set(result["invalidated_facts"]) == {f"{PREFIX}f1", f"{PREFIX}f2"}
    assert repo.get_fact(f"{PREFIX}f1") is None
    assert repo.get_fact(f"{PREFIX}f2") is None
    assert repo.get_fact_history(f"{PREFIX}f1")[0]["status"] == "obsolete"
    assert repo.get_fact_history(f"{PREFIX}f2")[0]["status"] == "obsolete"


def test_propagates_to_same_entity_inferred_dependents(
    repo: GraphRepository, pg_conn, neo4j_client: Neo4jClient, g67_user
) -> None:
    repo.create_entity(entity_id=f"{PREFIX}entity", label="Entity")
    create_source(neo4j_client, f"{PREFIX}src")
    repo.create_fact(
        fact_id=f"{PREFIX}parent",
        entity_id=f"{PREFIX}entity",
        property="state",
        value="a",
        source_id=f"{PREFIX}src",
        confidence="EXTRACTED",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}dep",
        entity_id=f"{PREFIX}entity",
        property="summary",
        value="inferred",
        source_id=None,
        confidence="INFERRED",
    )
    link_derived_from(neo4j_client, f"{PREFIX}parent", f"{PREFIX}src")

    result = invalidate_source(
        repo,
        pg_conn,
        f"{PREFIX}src",
        reason="test",
        user_id=str(g67_user["id"]),
    )

    assert result["invalidated_facts"] == [f"{PREFIX}parent"]
    assert result["under_review_facts"] == [f"{PREFIX}dep"]
    assert repo.get_fact(f"{PREFIX}parent") is None
    dep = repo.get_fact(f"{PREFIX}dep")
    assert dep is not None
    assert dep["status"] == "under_review"


def test_propagation_respects_depth_limit(
    repo: GraphRepository, pg_conn, neo4j_client: Neo4jClient, g67_user
) -> None:
    # P (EXTRACTED, derived from source) -> D1 -> D2 -> D3 (explicit edges).
    for suffix in ("e0", "e1", "e2", "e3"):
        repo.create_entity(entity_id=f"{PREFIX}{suffix}", label=suffix.upper())
    create_source(neo4j_client, f"{PREFIX}src")

    repo.create_fact(
        fact_id=f"{PREFIX}p",
        entity_id=f"{PREFIX}e0",
        property="state",
        value="a",
        source_id=f"{PREFIX}src",
        confidence="EXTRACTED",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}d1",
        entity_id=f"{PREFIX}e1",
        property="state",
        value="d1",
        source_id=None,
        confidence="INFERRED",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}d2",
        entity_id=f"{PREFIX}e2",
        property="state",
        value="d2",
        source_id=None,
        confidence="INFERRED",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}d3",
        entity_id=f"{PREFIX}e3",
        property="state",
        value="d3",
        source_id=None,
        confidence="INFERRED",
    )

    link_derived_from(neo4j_client, f"{PREFIX}p", f"{PREFIX}src")
    link_fact_to_fact(neo4j_client, f"{PREFIX}d1", f"{PREFIX}p")
    link_fact_to_fact(neo4j_client, f"{PREFIX}d2", f"{PREFIX}d1")
    link_fact_to_fact(neo4j_client, f"{PREFIX}d3", f"{PREFIX}d2")

    result = invalidate_source(
        repo,
        pg_conn,
        f"{PREFIX}src",
        reason="test",
        user_id=str(g67_user["id"]),
        max_depth=2,
    )

    assert result["invalidated_facts"] == [f"{PREFIX}p"]
    assert set(result["under_review_facts"]) == {f"{PREFIX}d1", f"{PREFIX}d2"}
    assert repo.get_fact(f"{PREFIX}d1")["status"] == "under_review"
    assert repo.get_fact(f"{PREFIX}d2")["status"] == "under_review"
    assert repo.get_fact(f"{PREFIX}d3")["status"] == "valid"


def test_invalidate_source_is_idempotent(
    repo: GraphRepository, pg_conn, neo4j_client: Neo4jClient, g67_user
) -> None:
    repo.create_entity(entity_id=f"{PREFIX}entity", label="Entity")
    create_source(neo4j_client, f"{PREFIX}src")
    repo.create_fact(
        fact_id=f"{PREFIX}f",
        entity_id=f"{PREFIX}entity",
        property="state",
        value="a",
        source_id=f"{PREFIX}src",
    )
    link_derived_from(neo4j_client, f"{PREFIX}f", f"{PREFIX}src")

    first = invalidate_source(
        repo, pg_conn, f"{PREFIX}src", reason="test", user_id=str(g67_user["id"])
    )
    assert first["invalidated_facts"] == [f"{PREFIX}f"]

    second = invalidate_source(
        repo,
        pg_conn,
        f"{PREFIX}src",
        reason="test again",
        user_id=str(g67_user["id"]),
    )
    assert second["invalidated_facts"] == []
    assert second["under_review_facts"] == []


def test_invalidate_missing_source_raises(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    with pytest.raises(SourceNotFoundError):
        invalidate_source(
            repo,
            pg_conn,
            f"{PREFIX}missing",
            reason="test",
            user_id=str(g67_user["id"]),
        )


def test_invalidate_rejects_invalid_depth(
    repo: GraphRepository, pg_conn, neo4j_client: Neo4jClient, g67_user
) -> None:
    create_source(neo4j_client, f"{PREFIX}src")
    with pytest.raises(InvalidationError):
        invalidate_source(
            repo,
            pg_conn,
            f"{PREFIX}src",
            reason="test",
            user_id=str(g67_user["id"]),
            max_depth=11,
        )
