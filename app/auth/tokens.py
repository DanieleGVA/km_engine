"""JWT access + refresh con rotazione e revoca su Postgres (ADR-002 D1).

- Access token: HS256, 15 min, claim sub/typ/roles/teams/tenant/iat/exp/jti.
  Non revocabile singolarmente (finestra 15 min accettata nel MVP).
- Refresh token: JWT 14 giorni (sub/typ/jti), hash SHA-256 in refresh_tokens.
  Rotazione ad ogni uso: il vecchio viene revocato; il riuso di un token
  revocato e' trattato come possibile furto e revoca a cascata tutte le
  sessioni attive dell'utente (schema senza family_id: la famiglia e'
  l'insieme dei token dell'utente).
- Lo stato di revoca vive in Postgres, non in memoria (app layer stateless).
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import psycopg

from .config import AuthSettings
from .errors import (
    InactiveUserError,
    InvalidCredentialsError,
    TokenError,
    TokenExpiredError,
    TokenReuseError,
)
from .hashing import verify_password
from .users import resolve_identity


def _hash_token(token: str) -> str:
    """Hash SHA-256 del refresh token: mai il token in chiaro nel DB."""
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def issue_access_token(
    user_id: uuid.UUID | str,
    *,
    roles: list[str] | tuple[str, ...],
    teams: list[str] | tuple[str, ...],
    tenant: str = "default",
    settings: AuthSettings | None = None,
    ttl: timedelta | None = None,
) -> str:
    """Firma un access token (claims OIDC-compatibili, ADR-002 D7)."""
    s = settings or AuthSettings()
    now = _now()
    claims = {
        "sub": str(user_id),
        "typ": "access",
        "roles": sorted(roles),
        "teams": sorted(teams),
        "tenant": tenant,
        "iat": now,
        "exp": now + (ttl or s.access_token_ttl),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, s.jwt_secret, algorithm=s.jwt_algorithm)


def decode_token(
    token: str, *, settings: AuthSettings | None = None, expected_type: str = "access"
) -> dict:
    """Valida firma, exp e typ del token; restituisce i claim."""
    s = settings or AuthSettings()
    try:
        claims = jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpiredError("Token scaduto.") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError(f"Token non valido: {exc}") from exc
    if claims.get("typ") != expected_type:
        raise TokenError(
            f"Tipo di token errato: atteso {expected_type!r}, ottenuto {claims.get('typ')!r}."
        )
    return claims


def _issue_refresh_token(
    conn: psycopg.Connection,
    user_id: uuid.UUID,
    settings: AuthSettings,
    ttl: timedelta | None = None,
) -> str:
    """Firma un refresh token e ne registra l'hash in refresh_tokens."""
    now = _now()
    exp = now + (ttl or settings.refresh_token_ttl)
    token = jwt.encode(
        {
            "sub": str(user_id),
            "typ": "refresh",
            "iat": now,
            "exp": exp,
            "jti": uuid.uuid4().hex,
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )
    conn.execute(
        "INSERT INTO refresh_tokens (user_id, token_hash, expires_at) VALUES (%s, %s, %s)",
        (user_id, _hash_token(token), exp),
    )
    return token


def login(
    conn: psycopg.Connection,
    username: str,
    password: str,
    *,
    settings: AuthSettings | None = None,
) -> dict:
    """Autentica utente (verifica constant-time) ed emette la coppia access+refresh."""
    s = settings or AuthSettings()
    row = conn.execute(
        "SELECT id, password_hash, active FROM users WHERE username = %s", (username,)
    ).fetchone()
    if row is None or not verify_password(password, row[1]):
        raise InvalidCredentialsError("Credenziali non valide.")
    if not row[2]:
        # utente disattivato: stesso errore del password errato, non si rivela lo stato
        raise InvalidCredentialsError("Credenziali non valide.")
    user_id: uuid.UUID = row[0]
    roles, teams = resolve_identity(conn, user_id)
    access = issue_access_token(
        user_id, roles=roles, teams=teams, tenant=s.tenant, settings=s
    )
    with conn.transaction():
        refresh = _issue_refresh_token(conn, user_id, s)
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": int(s.access_token_ttl.total_seconds()),
        "roles": roles,
        "teams": teams,
    }


def refresh(
    conn: psycopg.Connection, refresh_token: str, *, settings: AuthSettings | None = None
) -> dict:
    """Rinnova la sessione: valida il refresh, lo revoca e ne emette uno nuovo.

    Riuso di un token gia' revocato -> revoca a cascata di tutte le sessioni
    attive dell'utente (possibile furto), con commit persistito prima
    dell'errore. Utente disattivato -> tutte le sessioni revocate.
    """
    s = settings or AuthSettings()
    decode_token(refresh_token, settings=s, expected_type="refresh")
    token_hash = _hash_token(refresh_token)
    result: dict | None = None
    outcome = "ok"
    with conn.transaction():
        row = conn.execute(
            """
            SELECT id, user_id, revoked_at, expires_at
            FROM refresh_tokens WHERE token_hash = %s FOR UPDATE
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            outcome = "unknown"
        else:
            token_id, user_id, revoked_at, expires_at = row
            now = _now()
            if revoked_at is not None:
                # riuso rilevato: revoca a cascata (persistita dal commit del blocco)
                conn.execute(
                    """
                    UPDATE refresh_tokens SET revoked_at = %s, revoked_by = %s
                    WHERE user_id = %s AND revoked_at IS NULL
                    """,
                    (now, user_id, user_id),
                )
                outcome = "reuse"
            elif expires_at <= now:
                outcome = "expired"
            else:
                urow = conn.execute(
                    "SELECT active FROM users WHERE id = %s", (user_id,)
                ).fetchone()
                if urow is None or not urow[0]:
                    conn.execute(
                        "UPDATE refresh_tokens SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL",
                        (now, user_id),
                    )
                    outcome = "inactive"
                else:
                    # rotazione: revoca il token usato, emetti il nuovo (stessa transazione)
                    conn.execute(
                        "UPDATE refresh_tokens SET revoked_at = %s, revoked_by = %s WHERE id = %s",
                        (now, user_id, token_id),
                    )
                    new_refresh = _issue_refresh_token(conn, user_id, s)
                    roles, teams = resolve_identity(conn, user_id)
                    access = issue_access_token(
                        user_id, roles=roles, teams=teams, tenant=s.tenant, settings=s
                    )
                    result = {
                        "access_token": access,
                        "refresh_token": new_refresh,
                        "token_type": "bearer",
                        "expires_in": int(s.access_token_ttl.total_seconds()),
                        "roles": roles,
                        "teams": teams,
                    }
    if outcome == "reuse":
        raise TokenReuseError(
            "Riuso di refresh token gia' revocato: tutte le sessioni dell'utente sono state revocate."
        )
    if outcome == "expired":
        raise TokenExpiredError("Refresh token scaduto.")
    if outcome == "inactive":
        raise InactiveUserError("Utente disattivato: sessioni revocate.")
    if outcome == "unknown":
        raise TokenError("Refresh token non riconosciuto.")
    assert result is not None
    return result


def revoke_refresh(
    conn: psycopg.Connection,
    refresh_token: str,
    *,
    revoked_by: uuid.UUID | str | None = None,
) -> bool:
    """Revoca un refresh token esplicitamente (logout). True se era attivo."""
    rb = None
    if revoked_by is not None:
        rb = revoked_by if isinstance(revoked_by, uuid.UUID) else uuid.UUID(str(revoked_by))
    with conn.transaction():
        cur = conn.execute(
            """
            UPDATE refresh_tokens SET revoked_at = now(), revoked_by = %s
            WHERE token_hash = %s AND revoked_at IS NULL
            """,
            (rb, _hash_token(refresh_token)),
        )
    return cur.rowcount > 0


def logout(
    conn: psycopg.Connection, refresh_token: str, *, revoked_by: uuid.UUID | str | None = None
) -> bool:
    """Alias semantico di revoke_refresh (revoca solo la sessione passata)."""
    return revoke_refresh(conn, refresh_token, revoked_by=revoked_by)


def revoke_all_user_tokens(
    conn: psycopg.Connection, user_id: uuid.UUID | str
) -> int:
    """Logout-all: revoca tutti i refresh attivi dell'utente (FR4.5)."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    with conn.transaction():
        cur = conn.execute(
            "UPDATE refresh_tokens SET revoked_at = now() WHERE user_id = %s AND revoked_at IS NULL",
            (uid,),
        )
    return cur.rowcount


def list_refresh_tokens(
    conn: psycopg.Connection, user_id: uuid.UUID | str
) -> list[dict]:
    """Introspezione (admin/test): stato dei refresh token di un utente."""
    uid = user_id if isinstance(user_id, uuid.UUID) else uuid.UUID(str(user_id))
    rows = conn.execute(
        """
        SELECT id, token_hash, issued_at, expires_at, revoked_at
        FROM refresh_tokens WHERE user_id = %s ORDER BY issued_at
        """,
        (uid,),
    ).fetchall()
    return [
        {
            "id": r[0],
            "token_hash": r[1],
            "issued_at": r[2],
            "expires_at": r[3],
            "revoked_at": r[4],
        }
        for r in rows
    ]
