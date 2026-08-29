"""Test endpoint di servizio /auth/login, /auth/refresh, /auth/logout.

httpx non e' nel progetto (niente TestClient): gli handler async vengono
invocati direttamente con Request Starlette costruite a mano, corpo JSON
incluso. Il router resta montabile dall'app FastAPI del WP5.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import create_user
from app.auth import login as login_service
from app.auth.routes import LoginRequest, LogoutRequest, RefreshRequest
from app.auth.routes import login as login_route
from app.auth.routes import logout as logout_route
from app.auth.routes import refresh as refresh_route


def make_request(headers: dict[str, str] | None = None, json_body: dict | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    body = json.dumps(json_body).encode() if json_body is not None else b""
    scope = {"type": "http", "method": "POST", "path": "/auth/login", "headers": raw, "query_string": b""}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


@pytest.fixture()
def existing_user(conn):
    create_user(conn, "test_route_user", "test_route_user@example.test", "test-password-123", roles=("viewer",))
    return {"username": "test_route_user", "password": "test-password-123"}


class TestLoginRoute:
    @pytest.mark.asyncio
    async def test_login_returns_token_pair(self, existing_user, settings):
        resp = await login_route(LoginRequest(**existing_user), settings)
        assert resp.token_type == "bearer"
        assert resp.expires_in == 900
        assert resp.access_token and resp.refresh_token
        # il token emesso dalla route e' valido col middleware
        from app.auth import auth_required
        principal = await auth_required(
            make_request({"Authorization": f"Bearer {resp.access_token}"}), settings
        )
        assert principal.roles == ("viewer",)

    @pytest.mark.asyncio
    async def test_login_wrong_credentials_is_401(self, settings):
        body = LoginRequest(username="test_route_nobody", password="wrong-password-1")
        with pytest.raises(HTTPException) as exc:
            await login_route(body, settings)
        assert exc.value.status_code == 401


class TestRefreshRoute:
    @pytest.mark.asyncio
    async def test_refresh_rotates_token(self, conn, existing_user, settings):
        session = login_service(conn, existing_user["username"], existing_user["password"], settings=settings)
        resp = await refresh_route(RefreshRequest(refresh_token=session["refresh_token"]), settings)
        assert resp.refresh_token != session["refresh_token"]
        # il vecchio non e' piu' rinnovabile via route
        with pytest.raises(HTTPException) as exc:
            await refresh_route(RefreshRequest(refresh_token=session["refresh_token"]), settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_refresh_garbage_token_is_401(self, settings):
        with pytest.raises(HTTPException) as exc:
            await refresh_route(RefreshRequest(refresh_token="garbage"), settings)
        assert exc.value.status_code == 401


class TestLogoutRoute:
    @pytest.mark.asyncio
    async def test_logout_revokes_session(self, conn, existing_user, settings):
        session = login_service(conn, existing_user["username"], existing_user["password"], settings=settings)
        headers = {"Authorization": f"Bearer {session['access_token']}"}
        resp = await logout_route(LogoutRequest(refresh_token=session["refresh_token"]), make_request(headers), settings)
        assert resp == {"revoked": True}
        with pytest.raises(HTTPException) as exc:
            await refresh_route(RefreshRequest(refresh_token=session["refresh_token"]), settings)
        assert exc.value.status_code == 401

    @pytest.mark.asyncio
    async def test_logout_requires_authentication(self, conn, existing_user, settings):
        session = login_service(conn, existing_user["username"], existing_user["password"], settings=settings)
        with pytest.raises(HTTPException) as exc:
            await logout_route(LogoutRequest(refresh_token=session["refresh_token"]), make_request(), settings)
        assert exc.value.status_code == 401
