"""Endpoint di servizio /auth/login, /auth/refresh, /auth/logout (ADR-002 D6).

Unica parte pubblica dell'API (dietro rate limiting a livello nginx in prod).
L'app FastAPI vera (WP5) monta questo router; qui ogni handler apre una propria
connessione Postgres: l'app layer resta stateless e multi-istanza.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from .config import AuthSettings, get_auth_settings
from .db import connect
from .deps import auth_required
from .errors import AuthError, InvalidCredentialsError, TokenError
from .oidc import get_oidc_verifier, oidc_issue_tokens
from .tokens import login_async
from .tokens import logout as logout_service
from .tokens import refresh as refresh_service

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class OIDCLoginRequest(BaseModel):
    id_token: str
    nonce: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


def _map_error(exc: AuthError) -> HTTPException:
    """Errori auth -> status HTTP esplicito (401 credenziali/token, 403 utenza)."""
    if isinstance(exc, (InvalidCredentialsError, TokenError)):
        return HTTPException(status_code=401, detail=str(exc))
    return HTTPException(status_code=403, detail=str(exc))


@router.post("/login", response_model=TokenResponse)
async def login(
    body: LoginRequest, settings: Annotated[AuthSettings | None, Depends(get_auth_settings)] = None
) -> TokenResponse:
    """Public: username+password -> coppia access (15 min) + refresh (14 gg).

    WP-E1: l'hash argon2id viene eseguito fuori dall'event loop
    (``anyio.to_thread``) per non bloccare le altre richieste durante il
    login storm.
    """
    s = settings or get_auth_settings()
    try:
        with connect(s) as conn:
            result = await login_async(conn, body.username, body.password, settings=s)
    except AuthError as exc:
        raise _map_error(exc) from exc
    return TokenResponse(**result)


@router.post("/oidc/login", response_model=TokenResponse)
async def oidc_login(
    body: OIDCLoginRequest,
    settings: Annotated[AuthSettings | None, Depends(get_auth_settings)] = None,
) -> TokenResponse:
    """Public: id_token OIDC -> coppia access+refresh locale (WP-E1).

    L'utente locale deve già esistere (nessun auto-provisioning in questa
    iterazione). La verifica dell'id_token usa discovery+JWKS con cache.
    """
    s = settings or get_auth_settings()
    verifier = get_oidc_verifier()
    try:
        with connect(s) as conn:
            result = await oidc_issue_tokens(
                conn, body.id_token, nonce=body.nonce, settings=s, verifier=verifier
            )
    except AuthError as exc:
        raise _map_error(exc) from exc
    return TokenResponse(**result)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(
    body: RefreshRequest, settings: Annotated[AuthSettings | None, Depends(get_auth_settings)] = None
) -> TokenResponse:
    """Public: refresh token valido -> nuova coppia; rotazione con revoca del vecchio."""
    s = settings or get_auth_settings()
    try:
        with connect(s) as conn:
            result = refresh_service(conn, body.refresh_token, settings=s)
    except AuthError as exc:
        raise _map_error(exc) from exc
    return TokenResponse(**result)


@router.post("/logout")
async def logout(
    body: LogoutRequest,
    request: Request,
    settings: Annotated[AuthSettings | None, Depends(get_auth_settings)] = None,
) -> dict:
    """Autenticato: revoca il refresh token passato (la sessione non si rinnova piu')."""
    s = settings or get_auth_settings()
    principal = await auth_required(request, s)
    with connect(s) as conn:
        revoked = logout_service(
            conn, body.refresh_token, revoked_by=principal.user_id
        )
    return {"revoked": revoked}
