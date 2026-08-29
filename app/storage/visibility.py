"""Visibility attributes and inheritance helpers.

ADR-001 D4 stores visibility as flat properties on Entity and Fact:
``is_public`` (bool), ``roles`` (list[str]) and ``teams`` (list[str]).
Default is deny: a node without visibility attributes is never public.

Inheritance policy (simple, refined in WP5): each dimension is resolved
independently. A dimension explicitly set on a Fact wins over the Entity
dimension; an unset Fact dimension inherits the Entity value. If neither
node sets a dimension, the deny default applies.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Visibility:
    """Visibility specification.

    ``None`` means "not provided": the dimension is omitted in Neo4j and can
    inherit from the parent Entity. An empty tuple/list is an explicit empty
    value and wins over inheritance.
    """

    is_public: bool | None = None
    roles: tuple[str, ...] | None = None
    teams: tuple[str, ...] | None = None

    def to_props(self) -> dict[str, Any]:
        """Return Neo4j properties for the explicitly set dimensions only."""
        props: dict[str, Any] = {}
        if self.is_public is not None:
            props["is_public"] = self.is_public
        if self.roles is not None:
            props["roles"] = list(self.roles)
        if self.teams is not None:
            props["teams"] = list(self.teams)
        return props


def visibility_from_props(props: dict[str, Any]) -> Visibility:
    """Read concrete visibility values from a node property dict."""
    return Visibility(
        is_public=bool(props.get("is_public", False)),
        roles=tuple(props.get("roles") or ()),
        teams=tuple(props.get("teams") or ()),
    )


def effective_visibility(
    fact_props: dict[str, Any],
    entity_props: dict[str, Any],
) -> Visibility:
    """Resolve Fact visibility with Fact->Entity inheritance (explicit wins).

    ``fact_props`` and ``entity_props`` are raw node property dicts. A key is
    considered explicit only when present in the dict; missing keys inherit.
    The returned Visibility is concrete (no None fields).
    """
    if "is_public" in fact_props:
        is_public = bool(fact_props["is_public"])
    else:
        is_public = bool(entity_props.get("is_public", False))

    if "roles" in fact_props:
        roles = tuple(fact_props.get("roles") or ())
    else:
        roles = tuple(entity_props.get("roles") or ())

    if "teams" in fact_props:
        teams = tuple(fact_props.get("teams") or ())
    else:
        teams = tuple(entity_props.get("teams") or ())

    return Visibility(is_public=is_public, roles=roles, teams=teams)


def is_visible(
    visibility: Visibility,
    *,
    roles: Iterable[str] = (),
    teams: Iterable[str] = (),
    is_admin: bool = False,
    is_editor: bool = False,
) -> bool:
    """Return True when a principal can see content with ``visibility``.

    Admin/Editor bypass is a storage-level simplification; the authorized
    scope check is applied by the query engine in WP5.
    """
    if is_admin or is_editor:
        return True
    if visibility.is_public:
        return True
    principal_roles = set(roles)
    principal_teams = set(teams)
    node_roles = set(visibility.roles or ())
    node_teams = set(visibility.teams or ())
    return bool(
        (principal_roles and principal_roles.intersection(node_roles))
        or (principal_teams and principal_teams.intersection(node_teams))
    )


def apply_visibility(props: dict[str, Any], visibility: Visibility) -> dict[str, Any]:
    """Return a copy of ``props`` with only the explicit dimensions updated."""
    merged = dict(props)
    merged.update(visibility.to_props())
    return merged
