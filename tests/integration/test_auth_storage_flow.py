"""Gate G3 — flusso integrato login → identità → visibilità (auth → storage).

Copre il ponte richiesto dal gate: JWT reale (ADR-002 D1) → Principal
(ADR-002 D6) → contesto di visibilità → filtro storage (ADR-001 D4).
"""
from __future__ import annotations

import pytest
from starlette.requests import Request

from app.auth import (
    Principal,
    auth_required,
    decode_token,
    login,
    principal_from_claims,
    principal_visibility_context,
    refresh,
    resolve_identity,
)
from app.storage.visibility import Visibility, is_visible
from tests.integration.constants import TEAM_A, TEAM_B, TEST_PASSWORD


def bearer_header(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def make_request(headers: dict[str, str] | None = None) -> Request:
    """Request Starlette minimale (niente server HTTP: httpx non è nel progetto)."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": raw,
        "query_string": b"",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


class TestLoginIdentityVisibility:
    def test_login_emits_access_token_with_resolved_claims(
        self, conn, make_g3_user, settings
    ) -> None:
        user = make_g3_user("viewer_a", roles=("viewer",), teams=(TEAM_A,))
        session = login(conn, "g3_viewer_a", TEST_PASSWORD, settings=settings)
        claims = decode_token(session["access_token"], settings=settings)
        assert claims["typ"] == "access"
        assert claims["sub"] == str(user["id"])
        assert claims["roles"] == ["viewer"]
        assert claims["teams"] == [TEAM_A]
        assert claims["tenant"] == "default"
        assert claims["jti"]
        assert claims["exp"] > claims["iat"]

    def test_token_claims_agree_with_resolve_identity(
        self, conn, make_g3_user, settings
    ) -> None:
        make_g3_user("multi", roles=("viewer", "editor"), teams=(TEAM_A, TEAM_B))
        session = login(conn, "g3_multi", TEST_PASSWORD, settings=settings)
        claims = decode_token(session["access_token"], settings=settings)
        roles_db, teams_db = resolve_identity(conn, claims["sub"])
        # i claim del token sono lo snapshot dell'identità risolta dal DB (ADR-002 D2)
        assert claims["roles"] == sorted(roles_db)
        assert claims["teams"] == sorted(teams_db)

    def test_refresh_rotates_session_and_keeps_identity(
        self, conn, make_g3_user, settings
    ) -> None:
        make_g3_user("viewer_b", roles=("viewer",), teams=(TEAM_B,))
        first = login(conn, "g3_viewer_b", TEST_PASSWORD, settings=settings)
        first_sub = decode_token(first["access_token"], settings=settings)["sub"]
        second = refresh(conn, first["refresh_token"], settings=settings)
        claims = decode_token(second["access_token"], settings=settings)
        assert claims["sub"] == first_sub
        assert claims["typ"] == "access"
        assert claims["roles"] == ["viewer"]
        assert claims["teams"] == [TEAM_B]

    @pytest.mark.asyncio
    async def test_auth_required_resolves_principal_from_real_login(
        self, conn, make_g3_user, settings
    ) -> None:
        make_g3_user("admin", roles=("admin",))
        session = login(conn, "g3_admin", TEST_PASSWORD, settings=settings)
        principal = await auth_required(
            make_request(bearer_header(session["access_token"])), settings
        )
        assert isinstance(principal, Principal)
        assert principal.roles == ("admin",)
        assert principal.teams == ()
        assert principal.tenant == "default"
        assert principal.jti

    def test_principal_visibility_context_bridges_to_storage_filter(
        self, conn, make_g3_user, settings
    ) -> None:
        """Il contesto Principal alimenta is_visible (ADR-001 D4) senza toccare il token."""
        make_g3_user("viewer_a", roles=("viewer",), teams=(TEAM_A,))
        session = login(conn, "g3_viewer_a", TEST_PASSWORD, settings=settings)
        principal = principal_from_claims(
            decode_token(session["access_token"], settings=settings)
        )
        ctx = principal_visibility_context(principal)
        assert ctx["roles"] == ("viewer",)
        assert ctx["teams"] == (TEAM_A,)
        assert ctx["is_admin"] is False
        assert ctx["is_editor"] is False
        # viewer di teamA: vede il proprio team, non teamB, non default-deny
        assert is_visible(Visibility(teams=(TEAM_A,)), **ctx) is True
        assert is_visible(Visibility(teams=(TEAM_B,)), **ctx) is False
        assert is_visible(Visibility(is_public=True), **ctx) is True
        assert is_visible(Visibility(), **ctx) is False

    def test_admin_context_bypasses_default_deny(
        self, conn, make_g3_user, settings
    ) -> None:
        make_g3_user("admin", roles=("admin",))
        session = login(conn, "g3_admin", TEST_PASSWORD, settings=settings)
        principal = principal_from_claims(
            decode_token(session["access_token"], settings=settings)
        )
        ctx = principal_visibility_context(principal)
        assert ctx["is_admin"] is True
        # default-deny visibile solo a Admin/Editor con scope autorizzato (ADR-001 D4)
        assert is_visible(Visibility(), **ctx) is True
