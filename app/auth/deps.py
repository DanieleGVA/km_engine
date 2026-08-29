"""Dipendenze FastAPI: autenticazione Bearer + RBAC (ADR-002 D6).

Il middleware risolve identita' -> ruoli/teams/tenant -> Principal; il resto
dell'app vede solo Principal, mai i token (ADR-002 D7: logica legata ai claim,
non al formato di emissione).
"""
from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request

from .config import AuthSettings, get_auth_settings
from .errors import TokenError
from .tokens import decode_token


@dataclass(frozen=True)
class Principal:
    """Identita' risolta dal middleware: il contesto per query engine e storage."""

    user_id: str
    roles: tuple[str, ...]
    teams: tuple[str, ...]
    tenant: str
    jti: str


def principal_from_claims(claims: dict) -> Principal:
    """Costruisce il Principal dai claim di un access token valido."""
    return Principal(
        user_id=claims["sub"],
        roles=tuple(claims.get("roles", [])),
        teams=tuple(claims.get("teams", [])),
        tenant=claims.get("tenant", "default"),
        jti=claims.get("jti", ""),
    )


async def auth_required(
    request: Request, settings: AuthSettings | None = None
) -> Principal:
    """Dependency: richiede ``Authorization: Bearer <access JWT>`` valido."""
    s = settings or get_auth_settings()
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Autenticazione richiesta: header Authorization: Bearer <access token>.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(token, settings=s, expected_type="access")
    except TokenError as exc:
        raise HTTPException(
            status_code=401, detail=str(exc), headers={"WWW-Authenticate": "Bearer"}
        ) from exc
    return principal_from_claims(claims)


def require_roles(*required: str):
    """Dependency factory RBAC: l'utente deve avere almeno uno dei ruoli richiesti.

    Semantica di unione permissiva (ADR-002 D2): il permesso effettivo e'
    l'unione dei ruoli assegnati; nessun bypass implicito per admin.
    """
    required_set = frozenset(required)

    async def dependency(
        request: Request, settings: AuthSettings | None = None
    ) -> Principal:
        principal = await auth_required(request, settings)
        if not required_set.intersection(principal.roles):
            raise HTTPException(
                status_code=403,
                detail=f"Accesso negato: richiesto uno dei ruoli {sorted(required_set)},"
                f" l'utente ha {sorted(principal.roles)}.",
            )
        return principal

    return dependency
