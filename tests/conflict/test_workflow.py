"""Conflict resolution workflow tests (WP6, Gate G6)."""
from __future__ import annotations

import pytest

from app.conflict import (
    ConflictAlreadyResolvedError,
    ConflictNotFoundError,
    InvalidChoiceError,
    approve_conflict,
    reject_conflict,
    scan_conflicts,
)
from app.storage.repository import GraphRepository

from .conftest import list_g67_conflicts

PREFIX = "g67_"


def _setup_conflict(repo: GraphRepository, pg_conn) -> dict:
    repo.create_entity(entity_id=f"{PREFIX}entity", label="Entity")
    repo.create_fact(
        fact_id=f"{PREFIX}fact_a",
        entity_id=f"{PREFIX}entity",
        property="state",
        value="a",
        source_id=f"{PREFIX}src_a",
    )
    repo.create_fact(
        fact_id=f"{PREFIX}fact_b",
        entity_id=f"{PREFIX}entity",
        property="state",
        value="b",
        source_id=f"{PREFIX}src_b",
    )
    created = scan_conflicts(repo, pg_conn)
    assert len(created) == 1
    return created[0]


def test_approve_applies_chosen_and_invalidates_other(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    result = approve_conflict(
        repo, pg_conn, conflict["id"], "a", str(g67_user["id"])
    )

    assert result["status"] == "approved"
    assert result["resolved_by"] == str(g67_user["id"])
    assert result["resolved_at"] is not None

    # The chosen fact stays current; the losing fact is invalidated.
    assert repo.get_fact(f"{PREFIX}fact_a") is not None
    assert repo.get_fact(f"{PREFIX}fact_b") is None
    history_b = repo.get_fact_history(f"{PREFIX}fact_b")
    assert history_b[0]["status"] == "obsolete"
    assert history_b[0]["valid_to"] is not None


def test_reject_does_not_modify_graph(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    result = reject_conflict(pg_conn, conflict["id"], str(g67_user["id"]))

    assert result["status"] == "rejected"
    assert repo.get_fact(f"{PREFIX}fact_a") is not None
    assert repo.get_fact(f"{PREFIX}fact_b") is not None


def test_approve_invalid_choice_raises(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    with pytest.raises(InvalidChoiceError):
        approve_conflict(repo, pg_conn, conflict["id"], "c", str(g67_user["id"]))


def test_approve_already_resolved_raises(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    approve_conflict(repo, pg_conn, conflict["id"], "a", str(g67_user["id"]))
    with pytest.raises(ConflictAlreadyResolvedError):
        approve_conflict(repo, pg_conn, conflict["id"], "b", str(g67_user["id"]))


def test_approve_missing_conflict_raises(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    with pytest.raises(ConflictNotFoundError):
        approve_conflict(repo, pg_conn, 999999, "a", str(g67_user["id"]))


def test_list_conflicts_filter_status(
    repo: GraphRepository, pg_conn, g67_user
) -> None:
    conflict = _setup_conflict(repo, pg_conn)
    reject_conflict(pg_conn, conflict["id"], str(g67_user["id"]))

    assert list_g67_conflicts(pg_conn, status="pending") == []
    rejected = list_g67_conflicts(pg_conn, status="rejected")
    assert len(rejected) == 1
    assert rejected[0]["id"] == conflict["id"]
