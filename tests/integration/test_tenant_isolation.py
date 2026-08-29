"""Gate G3 — tenant isolation end-to-end su Neo4j (ADR-001 D4, FR4.3/NFR2).

Dataset: entità+fatto teamA-only, entità+fatto public, entità+fatto
default-deny, e un caso "esplicito vince" (entità pubblica con fatto
ristretto a teamA via is_public=False esplicito) più il caveat della
restrizione teams da sola (ADR-001, punto aperto 4). Per ogni Principal reale (JWT) il filtro di visibilità
viene applicato ai nodi letti da Neo4j, come farà il query engine in WP5.
"""
from __future__ import annotations

import pytest

from app.auth import (
    decode_token,
    login,
    principal_from_claims,
    principal_visibility_context,
)
from app.storage.visibility import (
    Visibility,
    effective_visibility,
    is_visible,
    visibility_from_props,
)
from tests.integration.constants import TEAM_A, TEAM_B, TEST_PASSWORD

ENTITY_IDS = [
    "g3_ent_team_a",
    "g3_ent_public",
    "g3_ent_deny",
    "g3_ent_pub_restricted_fact",
]


@pytest.fixture()
def seed_graph(repo):
    """Dataset di visibilità: teamA-only, public, default-deny, esplicito-vince."""
    repo.create_entity(
        entity_id="g3_ent_team_a",
        label="Secreto teamA",
        visibility=Visibility(teams=(TEAM_A,)),
    )
    repo.create_fact(
        fact_id="g3_fact_team_a",
        entity_id="g3_ent_team_a",
        property="status",
        value="active-team-a",
    )
    repo.create_entity(
        entity_id="g3_ent_public",
        label="Entity pubblica",
        visibility=Visibility(is_public=True),
    )
    repo.create_fact(
        fact_id="g3_fact_public",
        entity_id="g3_ent_public",
        property="status",
        value="public-value",
    )
    repo.create_entity(entity_id="g3_ent_deny", label="Entity senza visibilità")
    repo.create_fact(
        fact_id="g3_fact_deny",
        entity_id="g3_ent_deny",
        property="status",
        value="deny-value",
    )
    # esplicito vince (ADR-001 D4): entità pubblica, fatto con is_public=False
    # esplicito e teams=[teamA] -> il fatto è teamA-only, l'entità resta pubblica
    repo.create_entity(
        entity_id="g3_ent_pub_restricted_fact",
        label="Entity pubblica con fatto ristretto",
        visibility=Visibility(is_public=True),
    )
    repo.create_fact(
        fact_id="g3_fact_restricted",
        entity_id="g3_ent_pub_restricted_fact",
        property="status",
        value="team-a-only-fact",
        visibility=Visibility(is_public=False, teams=(TEAM_A,)),
    )
    # caveat storage (da validare in WP5, ADR-001 punto aperto 4): un fatto con
    # SOLO teams=[teamA] su entità pubblica eredita is_public=True per dimensione
    repo.create_fact(
        fact_id="g3_fact_team_restriction_only",
        entity_id="g3_ent_pub_restricted_fact",
        property="status",
        value="restriction-without-public-false",
        visibility=Visibility(teams=(TEAM_A,)),
    )


def _visibility_report(repo, ctx: dict) -> dict[str, bool]:
    """Applica il filtro di visibilità ai nodi letti da Neo4j (filtro WP5)."""
    report: dict[str, bool] = {}
    for entity_id in ENTITY_IDS:
        entity = repo.get_entity(entity_id)
        assert entity is not None, f"entità {entity_id} mancante nel grafo"
        report[f"entity:{entity_id}"] = is_visible(
            visibility_from_props(entity), **ctx
        )
        for fact in repo.get_facts_for_entity(entity_id):
            effective = effective_visibility(fact, entity)
            report[f"fact:{fact['logical_id']}"] = is_visible(effective, **ctx)
    return report


def _login_principal(conn, settings, username: str):
    """Login reale (JWT) e Principal risolto dai claim (ADR-002 D6)."""
    session = login(conn, username, TEST_PASSWORD, settings=settings)
    claims = decode_token(session["access_token"], settings=settings)
    return principal_from_claims(claims)


class TestTenantIsolation:
    def test_team_a_sees_own_data_and_public_only(
        self, conn, make_g3_user, repo, seed_graph, settings
    ) -> None:
        make_g3_user("viewer_a", roles=("viewer",), teams=(TEAM_A,))
        report = _visibility_report(
            repo, principal_visibility_context(_login_principal(conn, settings, "g3_viewer_a"))
        )
        assert report["entity:g3_ent_team_a"] is True
        assert report["fact:g3_fact_team_a"] is True
        assert report["entity:g3_ent_public"] is True
        assert report["fact:g3_fact_public"] is True
        assert report["entity:g3_ent_deny"] is False
        assert report["fact:g3_fact_deny"] is False
        assert report["entity:g3_ent_pub_restricted_fact"] is True
        assert report["fact:g3_fact_restricted"] is True

    def test_team_b_does_not_see_team_a_data(
        self, conn, make_g3_user, repo, seed_graph, settings
    ) -> None:
        make_g3_user("viewer_b", roles=("viewer",), teams=(TEAM_B,))
        report = _visibility_report(
            repo, principal_visibility_context(_login_principal(conn, settings, "g3_viewer_b"))
        )
        # tenant isolation: i dati teamA sono invisibili a teamB
        assert report["entity:g3_ent_team_a"] is False
        assert report["fact:g3_fact_team_a"] is False
        # solo il contenuto pubblico resta visibile
        assert report["entity:g3_ent_public"] is True
        assert report["fact:g3_fact_public"] is True
        # esplicito vince: fatto teamA nascosto anche su entità pubblica
        assert report["entity:g3_ent_pub_restricted_fact"] is True
        assert report["fact:g3_fact_restricted"] is False
        assert report["entity:g3_ent_deny"] is False
        assert report["fact:g3_fact_deny"] is False

    def test_viewer_without_team_sees_only_public(
        self, conn, make_g3_user, repo, seed_graph, settings
    ) -> None:
        make_g3_user("viewer_none", roles=("viewer",))
        report = _visibility_report(
            repo, principal_visibility_context(_login_principal(conn, settings, "g3_viewer_none"))
        )
        assert report["entity:g3_ent_team_a"] is False
        assert report["fact:g3_fact_team_a"] is False
        assert report["entity:g3_ent_public"] is True
        assert report["fact:g3_fact_public"] is True
        assert report["entity:g3_ent_pub_restricted_fact"] is True
        assert report["fact:g3_fact_restricted"] is False
        assert report["entity:g3_ent_deny"] is False
        assert report["fact:g3_fact_deny"] is False

    def test_admin_sees_everything_including_default_deny(
        self, conn, make_g3_user, repo, seed_graph, settings
    ) -> None:
        make_g3_user("admin", roles=("admin",))
        report = _visibility_report(
            repo, principal_visibility_context(_login_principal(conn, settings, "g3_admin"))
        )
        assert report, "report vuoto: il grafo non contiene nodi g3_"
        for key, visible in report.items():
            assert visible is True, f"{key} non visibile all'admin"

    def test_teams_restriction_alone_keeps_inherited_publicity(
        self, conn, make_g3_user, repo, seed_graph, settings
    ) -> None:
        """Caveat ADR-001 (punto aperto 4) da decidere in WP5.

        Nel resolver di storage ogni dimensione è indipendente: un fatto con
        SOLO teams=[teamA] su entità pubblica resta visibile pubblicamente
        perché is_public non è esplicito e viene ereditato True. Se WP5 vuole
        che una restrizione teams impliciti tolga la pubblicità, il query
        engine deve aggiungere la regola (il resolver non lo fa).
        """
        make_g3_user("viewer_b", roles=("viewer",), teams=(TEAM_B,))
        report = _visibility_report(
            repo, principal_visibility_context(_login_principal(conn, settings, "g3_viewer_b"))
        )
        # fatto con is_public=False esplicito + teams=[teamA]: non visibile a teamB
        assert report["fact:g3_fact_restricted"] is False
        # fatto con SOLO teams=[teamA] (is_public ereditato True): visibile a teamB
        assert report["fact:g3_fact_team_restriction_only"] is True

    def test_graph_data_is_physically_present_for_every_principal(
        self, repo, seed_graph
    ) -> None:
        """L'isolamento è logico/applicativo (ADR-002 D3): i nodi esistono nel grafo."""
        for entity_id in ENTITY_IDS:
            entity = repo.get_entity(entity_id)
            assert entity is not None, f"entità {entity_id} mancante"
            facts = repo.get_facts_for_entity(entity_id)
            assert facts, f"fatti mancanti per {entity_id}"
        assert repo.get_fact("g3_fact_team_a")["value"] == "active-team-a"
        assert repo.get_fact("g3_fact_deny")["value"] == "deny-value"
