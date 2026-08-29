"""CRUD tests for Entity, Fact and RELATES_TO."""

from __future__ import annotations

import pytest

from app.storage.errors import AlreadyExistsError, NotFoundError, ValidationError
from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility


def test_create_and_get_entity(repo: GraphRepository) -> None:
    entity = repo.create_entity(
        entity_id="wp2test_crud_entity_1",
        label="auth.py",
        type="code",
        source_file="src/auth.py",
        source_location="L1",
        confidence="EXTRACTED",
    )
    assert entity["id"] == "wp2test_crud_entity_1"
    assert entity["label"] == "auth.py"

    fetched = repo.get_entity("wp2test_crud_entity_1")
    assert fetched is not None
    assert fetched["id"] == entity["id"]
    assert fetched["type"] == "code"
    assert fetched["confidence"] == "EXTRACTED"


def test_create_entity_duplicate_raises(repo: GraphRepository) -> None:
    repo.create_entity(entity_id="wp2test_crud_entity_dup", label="dup")
    with pytest.raises(AlreadyExistsError):
        repo.create_entity(entity_id="wp2test_crud_entity_dup", label="dup")


def test_get_entity_missing_returns_none(repo: GraphRepository) -> None:
    assert repo.get_entity("wp2test_crud_entity_missing") is None


def test_update_entity_in_place(repo: GraphRepository) -> None:
    repo.create_entity(
        entity_id="wp2test_crud_entity_upd",
        label="old.py",
        type="code",
        confidence="EXTRACTED",
    )
    updated = repo.update_entity(
        "wp2test_crud_entity_upd",
        label="new.py",
        confidence="AMBIGUOUS",
        visibility=Visibility(is_public=True, roles=("eng",), teams=()),
    )
    assert updated["label"] == "new.py"
    assert updated["confidence"] == "AMBIGUOUS"
    assert updated["is_public"] is True
    assert updated["roles"] == ["eng"]


def test_update_entity_missing_raises(repo: GraphRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.update_entity("wp2test_crud_entity_upd_missing", label="x")


def test_create_and_get_fact(repo: GraphRepository) -> None:
    repo.create_entity(entity_id="wp2test_crud_entity_fact", label="module")
    fact = repo.create_fact(
        fact_id="wp2test_crud_fact_1",
        entity_id="wp2test_crud_entity_fact",
        property="exports",
        value="login",
        confidence="EXTRACTED",
        status="valid",
    )
    assert fact["id"] == "wp2test_crud_fact_1"
    assert fact["logical_id"] == "wp2test_crud_fact_1"
    assert fact["valid_to"] is None
    assert fact["status"] == "valid"

    fetched = repo.get_fact("wp2test_crud_fact_1")
    assert fetched is not None
    assert fetched["value"] == "login"
    assert fetched["property"] == "exports"


def test_create_fact_missing_entity_raises(repo: GraphRepository) -> None:
    with pytest.raises(NotFoundError):
        repo.create_fact(
            fact_id="wp2test_crud_fact_orphan",
            entity_id="wp2test_crud_entity_missing",
            property="p",
            value="v",
        )


def test_create_fact_duplicate_raises(repo: GraphRepository) -> None:
    repo.create_entity(entity_id="wp2test_crud_entity_fact_dup", label="module")
    repo.create_fact(
        fact_id="wp2test_crud_fact_dup",
        entity_id="wp2test_crud_entity_fact_dup",
        property="p",
        value="v",
    )
    with pytest.raises(AlreadyExistsError):
        repo.create_fact(
            fact_id="wp2test_crud_fact_dup",
            entity_id="wp2test_crud_entity_fact_dup",
            property="p",
            value="v2",
        )


def test_get_fact_missing_returns_none(repo: GraphRepository) -> None:
    assert repo.get_fact("wp2test_crud_fact_missing") is None


def test_create_and_get_relation(repo: GraphRepository) -> None:
    repo.create_entity(entity_id="wp2test_crud_rel_a", label="a")
    repo.create_entity(entity_id="wp2test_crud_rel_b", label="b")
    rel = repo.create_relation(
        source_entity_id="wp2test_crud_rel_a",
        target_entity_id="wp2test_crud_rel_b",
        relation="imports_from",
        confidence="EXTRACTED",
    )
    assert rel["source_id"] == "wp2test_crud_rel_a"
    assert rel["target_id"] == "wp2test_crud_rel_b"
    assert rel["relation"] == "imports_from"
    assert rel["valid_to"] is None

    fetched = repo.get_relation(
        "wp2test_crud_rel_a", "wp2test_crud_rel_b", "imports_from"
    )
    assert fetched is not None
    assert fetched["confidence"] == "EXTRACTED"

    relations = repo.get_relations("wp2test_crud_rel_a")
    assert len(relations) == 1
    assert relations[0]["relation"] == "imports_from"


def test_create_relation_missing_entity_raises(repo: GraphRepository) -> None:
    repo.create_entity(entity_id="wp2test_crud_rel_only", label="only")
    with pytest.raises(NotFoundError):
        repo.create_relation(
            source_entity_id="wp2test_crud_rel_only",
            target_entity_id="wp2test_crud_rel_missing",
            relation="uses",
        )


def test_invalid_status_and_confidence_raise(repo: GraphRepository) -> None:
    repo.create_entity(entity_id="wp2test_crud_entity_validation", label="v")
    with pytest.raises(ValidationError):
        repo.create_fact(
            fact_id="wp2test_crud_fact_bad_status",
            entity_id="wp2test_crud_entity_validation",
            property="p",
            value="v",
            status="deleted",
        )
    with pytest.raises(ValidationError):
        repo.create_fact(
            fact_id="wp2test_crud_fact_bad_conf",
            entity_id="wp2test_crud_entity_validation",
            property="p",
            value="v",
            confidence="MAYBE",
        )
