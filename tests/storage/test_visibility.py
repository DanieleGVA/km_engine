"""Visibility storage and inheritance tests (ADR-001 D4)."""

from __future__ import annotations

from app.storage.repository import GraphRepository
from app.storage.visibility import Visibility, effective_visibility, is_visible


def test_entity_without_visibility_is_default_deny(repo: GraphRepository) -> None:
    entity = repo.create_entity(entity_id="wp2test_vis_entity_default", label="x")
    assert "is_public" not in entity
    assert "roles" not in entity
    assert "teams" not in entity

    resolved = effective_visibility({}, entity)
    assert resolved.is_public is False
    assert resolved.roles == ()
    assert resolved.teams == ()
    assert is_visible(resolved, roles=("viewer",), teams=("team-a",)) is False


def test_entity_explicit_visibility_is_stored(repo: GraphRepository) -> None:
    entity = repo.create_entity(
        entity_id="wp2test_vis_entity_explicit",
        label="x",
        visibility=Visibility(is_public=True, roles=("eng",), teams=("team-a",)),
    )
    assert entity["is_public"] is True
    assert entity["roles"] == ["eng"]
    assert entity["teams"] == ["team-a"]


def test_fact_inherits_entity_visibility(repo: GraphRepository) -> None:
    entity = repo.create_entity(
        entity_id="wp2test_vis_entity_inherit",
        label="x",
        visibility=Visibility(is_public=False, roles=("eng",), teams=("team-a",)),
    )
    fact = repo.create_fact(
        fact_id="wp2test_vis_fact_inherit",
        entity_id="wp2test_vis_entity_inherit",
        property="p",
        value="v",
    )
    assert "is_public" not in fact
    resolved = effective_visibility(fact, entity)
    assert resolved.is_public is False
    assert resolved.roles == ("eng",)
    assert resolved.teams == ("team-a",)


def test_fact_explicit_visibility_wins_per_dimension(repo: GraphRepository) -> None:
    entity = repo.create_entity(
        entity_id="wp2test_vis_entity_explicit_win",
        label="x",
        visibility=Visibility(is_public=True, roles=("eng",), teams=("team-a",)),
    )
    fact = repo.create_fact(
        fact_id="wp2test_vis_fact_explicit_win",
        entity_id="wp2test_vis_entity_explicit_win",
        property="p",
        value="v",
        visibility=Visibility(is_public=False, roles=("legal",)),
    )
    resolved = effective_visibility(fact, entity)
    # is_public and roles are explicit on the Fact; teams is inherited.
    assert resolved.is_public is False
    assert resolved.roles == ("legal",)
    assert resolved.teams == ("team-a",)


def test_is_visible_rules(repo: GraphRepository) -> None:
    public = Visibility(is_public=True)
    role_only = Visibility(roles=("eng",))
    team_only = Visibility(teams=("team-a",))
    deny = Visibility()

    assert is_visible(public, roles=("viewer",)) is True
    assert is_visible(role_only, roles=("eng",)) is True
    assert is_visible(role_only, roles=("viewer",)) is False
    assert is_visible(team_only, teams=("team-a",)) is True
    assert is_visible(team_only, teams=("team-b",)) is False
    assert is_visible(deny, roles=("viewer",), teams=("team-a",)) is False
    assert is_visible(deny, roles=("viewer",), is_admin=True) is True
    assert is_visible(deny, roles=("viewer",), is_editor=True) is True
