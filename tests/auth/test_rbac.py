"""Test RBAC: auth_required e require_roles (gate G2, ADR-002 D2/D6)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import Principal, auth_required, login, require_roles
from app.auth.deps import principal_from_claims


def make_request(headers: dict[str, str] | None = None) -> Request:
    """Request Starlette minimale (niente server HTTP: httpx non e' nel progetto)."""
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "POST", "path": "/", "headers": raw, "query_string": b""}

    async def receive():  # corpo vuoto
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(scope, receive)


def bearer(result_or_token: dict | str) -> dict[str, str]:
    token = result_or_token["access_token"] if isinstance(result_or_token, dict) else result_or_token
    return {"Authorization": f"Bearer {token}"}


class TestAuthRequired:
    @pytest.mark.asyncio
    async def test_valid_token_resolves_principal(self, conn, make_user, settings):
        make_user("nia", roles=("editor",), teams=("test_team_eng",))
        session = login(conn, "test_nia", "test-password-123", settings=settings)
        principal = await auth_required(make_request(bearer(session)), settings)
        assert isinstance(principal, Principal)
        assert principal.user_id
        assert principal.roles == ("editor",)
        assert principal.teams == ("test_team_eng",)
        assert principal.tenant == "default"
        assert principal.jti

    @pytest.mark.asyncio
    async def test_missing_header_is_401(self, settings):
        with pytest.raises(HTTPException) as exc:
            await auth_required(make_request(), settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_garbage_token_is_401(self, settings):
        req = make_request({"Authorization": "Bearer not-a-jwt"})
        with pytest.raises(HTTPException) as exc:
            await auth_required(req, settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_token_as_access_is_401(self, conn, make_user, settings):
        make_user("omar")
        session = login(conn, "test_omar", "test-password-123", settings=settings)
        req = make_request(bearer(session["refresh_token"]))
        with pytest.raises(HTTPException) as exc:
            await auth_required(req, settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_expired_access_token_is_401(self, conn, make_user, settings):
        from datetime import timedelta

        from app.auth import issue_access_token
        user = make_user("paula")
        token = issue_access_token(user["id"], roles=["viewer"], teams=[], settings=settings, ttl=timedelta(minutes=-1))
        with pytest.raises(HTTPException) as exc:
            await auth_required(make_request(bearer(token)), settings)
        assert exc.value.status_code == 401


class TestRequireRoles:
    @pytest.mark.asyncio
    async def test_allowed_role_passes(self, conn, make_user, settings):
        make_user("quinn", roles=("editor", "viewer",))
        session = login(conn, "test_quinn", "test-password-123", settings=settings)
        dep = require_roles("editor")
        principal = await dep(make_request(bearer(session)), settings)
        assert principal.roles == ("editor", "viewer")

    @pytest.mark.asyncio
    async def test_missing_role_is_403_not_401(self, conn, make_user, settings):
        make_user("rosa", roles=("viewer",))
        session = login(conn, "test_rosa", "test-password-123", settings=settings)
        dep = require_roles("admin")
        with pytest.raises(HTTPException) as exc:
            await dep(make_request(bearer(session)), settings)
        assert exc.value.status_code == 403

    @pytest.mark.asyncio
    async def test_no_auth_is_401_before_role_check(self, settings):
        dep = require_roles("admin")
        with pytest.raises(HTTPException) as exc:
            await dep(make_request(), settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_multi_role_union_semantics(self, conn, make_user, settings):
        make_user("silvia", roles=("viewer", "ingestor"))
        session = login(conn, "test_silvia", "test-password-123", settings=settings)
        # il permesso effettivo e' l'unione dei ruoli (ADR-002 D2)
        for role in ("viewer", "ingestor"):
            principal = await require_roles(role)(make_request(bearer(session)), settings)
            assert principal.user_id


class TestPrincipalFromClaims:
    def test_principal_defaults_for_missing_optional_claims(self):
        principal = principal_from_claims({"sub": "u1", "typ": "access"})
        assert principal == Principal("u1", (), (), "default", "")
