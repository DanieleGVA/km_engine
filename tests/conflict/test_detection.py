"""Conflict detection tests (WP6, Gate G6)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.conflict import (
    detect_conflicts_for_entity,
    post_ingest_hook,
    scan_conflicts,
)
from app.storage.client import Neo4jClient
from app.storage.repository import GraphRepository

from .conftest import create_source, list_g67_conflicts

PREFIX = "g67_"


def _make_entity(repo: GraphRepository, entity_id: str = f"{PREFIX}entity") -> None:
    repo.create_entity(entity_id=entity_id, label="Entity", type="code")


def _make_fact(
    repo: GraphRepository,
    fact_id: str,
    entity_id: str,
    value: str,
    source_id: str,
    confidence: str = "EXTRACTED",
) -> None:
    repo.create_fact(
        fact_id=fact_id,
        entity_id=entity_id,
        property="state",
        value=value,
        source_id=source_id,
        confidence=confidence,
    )


def test_scan_detects_conflict_and_inserts_pending(
    repo: GraphRepository, pg_conn
) -> None:
    _make_entity(repo)
    _make_fact(repo, f"{PREFIX}fact_a", f"{PREFIX}entity", "active", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}fact_b", f"{PREFIX}entity", "inactive", f"{PREFIX}src_b")

    created = scan_conflicts(repo, pg_conn)
    assert len(created) == 1
    conflict = created[0]
    assert conflict["entity_id"] == f"{PREFIX}entity"
    assert conflict["property"] == "state"
    assert {conflict["value_a"], conflict["value_b"]} == {"active", "inactive"}
    assert {conflict["source_a"], conflict["source_b"]} == {
        f"{PREFIX}src_a",
        f"{PREFIX}src_b",
    }
    assert conflict["status"] == "pending"
    assert conflict["suggestion"]

    rows = list_g67_conflicts(pg_conn)
    assert len(rows) == 1
    assert rows[0]["id"] == conflict["id"]


def test_scan_skips_same_value_or_same_source(
    repo: GraphRepository, pg_conn
) -> None:
    # Same value, different sources -> not a conflict.
    _make_entity(repo, f"{PREFIX}entity_same_value")
    _make_fact(repo, f"{PREFIX}f1", f"{PREFIX}entity_same_value", "same", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}f2", f"{PREFIX}entity_same_value", "same", f"{PREFIX}src_b")

    # Different values, same source -> not a conflict.
    _make_entity(repo, f"{PREFIX}entity_same_source")
    _make_fact(repo, f"{PREFIX}f3", f"{PREFIX}entity_same_source", "x", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}f4", f"{PREFIX}entity_same_source", "y", f"{PREFIX}src_a")

    assert scan_conflicts(repo, pg_conn) == []


def test_scan_dedups_already_open_pending_conflicts(
    repo: GraphRepository, pg_conn
) -> None:
    _make_entity(repo)
    _make_fact(repo, f"{PREFIX}fact_a", f"{PREFIX}entity", "a", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}fact_b", f"{PREFIX}entity", "b", f"{PREFIX}src_b")

    first = scan_conflicts(repo, pg_conn)
    assert len(first) == 1

    second = scan_conflicts(repo, pg_conn)
    assert second == []
    assert len(list_g67_conflicts(pg_conn)) == 1


def test_suggestion_prefers_higher_confidence(
    repo: GraphRepository, pg_conn
) -> None:
    _make_entity(repo)
    _make_fact(
        repo, f"{PREFIX}fact_a", f"{PREFIX}entity", "a", f"{PREFIX}src_a",
        confidence="EXTRACTED",
    )
    _make_fact(
        repo, f"{PREFIX}fact_b", f"{PREFIX}entity", "b", f"{PREFIX}src_b",
        confidence="INFERRED",
    )

    created = scan_conflicts(repo, pg_conn)
    assert created[0]["suggestion"].startswith("a")


def test_suggestion_prefers_recent_source_on_equal_confidence(
    repo: GraphRepository, pg_conn, neo4j_client: Neo4jClient
) -> None:
    _make_entity(repo)
    _make_fact(repo, f"{PREFIX}fact_a", f"{PREFIX}entity", "a", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}fact_b", f"{PREFIX}entity", "b", f"{PREFIX}src_b")

    now = datetime.now(UTC)
    create_source(neo4j_client, f"{PREFIX}src_a", ingested_at=now - timedelta(days=2))
    create_source(neo4j_client, f"{PREFIX}src_b", ingested_at=now)

    created = scan_conflicts(repo, pg_conn)
    assert created[0]["suggestion"].startswith("b")


def test_post_ingest_hook_scans_multiple_entities(
    repo: GraphRepository, pg_conn
) -> None:
    _make_entity(repo, f"{PREFIX}hook_1")
    _make_entity(repo, f"{PREFIX}hook_2")
    _make_fact(repo, f"{PREFIX}hook_f1a", f"{PREFIX}hook_1", "a", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}hook_f1b", f"{PREFIX}hook_1", "b", f"{PREFIX}src_b")
    _make_fact(repo, f"{PREFIX}hook_f2a", f"{PREFIX}hook_2", "a", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}hook_f2b", f"{PREFIX}hook_2", "b", f"{PREFIX}src_b")

    created = post_ingest_hook(
        repo, pg_conn, [f"{PREFIX}hook_1", f"{PREFIX}hook_2"]
    )
    assert len(created) == 2
    assert {c["entity_id"] for c in created} == {f"{PREFIX}hook_1", f"{PREFIX}hook_2"}


def test_detect_conflicts_for_entity_is_scoped(
    repo: GraphRepository, pg_conn
) -> None:
    _make_entity(repo, f"{PREFIX}entity_1")
    _make_entity(repo, f"{PREFIX}entity_2")
    _make_fact(repo, f"{PREFIX}f1a", f"{PREFIX}entity_1", "a", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}f1b", f"{PREFIX}entity_1", "b", f"{PREFIX}src_b")
    _make_fact(repo, f"{PREFIX}f2a", f"{PREFIX}entity_2", "a", f"{PREFIX}src_a")
    _make_fact(repo, f"{PREFIX}f2b", f"{PREFIX}entity_2", "b", f"{PREFIX}src_b")

    created = detect_conflicts_for_entity(repo, pg_conn, f"{PREFIX}entity_1")
    assert len(created) == 1
    assert created[0]["entity_id"] == f"{PREFIX}entity_1"
