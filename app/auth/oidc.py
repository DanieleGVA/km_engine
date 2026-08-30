"""OIDC id_token verification (WP-E1, GE1).

Production path: discover the provider (``discovery_url``), fetch the JWKS,
cache it, and verify ``id_token`` claims (``iss``, ``aud``, ``exp``, ``nonce``).
The verifier is transport-agnostic: pass an ``http_client`` with an async
``get(url) -> dict`` method to avoid real network access in tests.

``FakeOIDCProvider`` is a deterministic offline provider for tests. It signs
tokens with HS256 and exposes an ``oct`` JWKS key. Production deployments must
use RS256 (or another asymmetric algorithm) and a real IdP; the fake provider
is test-only and never used by the application defaults.
"""
from __future__ import annotations

import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import jwt
import psycopg
from pydantic_settings import BaseSettings, SettingsConfigDict

from .config import AuthSettings
from .deps import Principal
from .errors import InvalidCredentialsError, TokenError
from .tokens import issue_token_pair
from .users import resolve_identity


class OIDCSettings(BaseSettings):
    """OIDC verifier settings (env prefix ``KM_OIDC_``)."""

    model_config = SettingsConfigDict(
        env_prefix="KM_OIDC_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    issuer: str = ""
    client_id: str = ""
    discovery_url: str = ""
    jwks_uri: str = ""
    algorithms: list[str] = ["RS256"]
    jwks_cache_ttl: int = 300


class OIDCHttpClient(Protocol):
    """Minimal async HTTP contract used by :class:`OIDCVerifier`."""

    async def get(self, url: str) -> dict[str, Any]:
        """Return the parsed JSON body for ``url``."""
        ...


def _now_ts() -> float:
    return time.monotonic()


def _b64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class OIDCVerifier:
    """Discover, fetch and verify OIDC id_tokens.

    ``http_client`` is optional. When omitted, a real ``httpx.AsyncClient`` is
    used (network access). Tests inject a fake client (no network).
    """

    def __init__(
        self,
        settings: OIDCSettings | None = None,
        *,
        http_client: OIDCHttpClient | None = None,
    ) -> None:
        self.settings = settings or OIDCSettings()
        self._http_client = http_client
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_fetched_at: float | None = None

    async def _get_json(self, url: str) -> dict[str, Any]:
        if self._http_client is not None:
            data = await self._http_client.get(url)
            if isinstance(data, dict):
                return data
            return data.json()  # type: ignore[union-attr]
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.json()

    async def discover(self) -> dict[str, Any]:
        """Return the OIDC discovery document (cached in-process)."""
        if self._discovery is not None:
            return self._discovery
        url = self.settings.discovery_url
        if not url:
            raise TokenError("OIDC discovery_url non configurato.")
        doc = await self._get_json(url)
        if not isinstance(doc, dict):
            raise TokenError("OIDC discovery document non valido.")
        self._discovery = doc
        return doc

    async def get_jwks(self, *, force: bool = False) -> dict[str, Any]:
        """Return the JWKS document (cached for ``jwks_cache_ttl`` seconds)."""
        now = _now_ts()
        if (
            not force
            and self._jwks is not None
            and self._jwks_fetched_at is not None
            and (now - self._jwks_fetched_at) < self.settings.jwks_cache_ttl
        ):
            return self._jwks

        jwks_uri = self.settings.jwks_uri
        if not jwks_uri:
            discovery = await self.discover()
            jwks_uri = discovery.get("jwks_uri", "")
        if not jwks_uri:
            raise TokenError("OIDC jwks_uri non configurato o assente nel discovery.")

        doc = await self._get_json(jwks_uri)
        if not isinstance(doc, dict) or "keys" not in doc:
            raise TokenError("OIDC JWKS non valido: campo 'keys' assente.")
        self._jwks = doc
        self._jwks_fetched_at = now
        return doc

    def _find_key(self, jwks: dict[str, Any], kid: str | None) -> dict[str, Any] | None:
        keys = jwks.get("keys", [])
        if kid is not None:
            for key in keys:
                if key.get("kid") == kid:
                    return key
        if len(keys) == 1:
            return keys[0]
        return None

    def _key_material(self, key: dict[str, Any], algorithm: str) -> Any:
        kty = key.get("kty")
        if kty == "oct":
            k = key.get("k")
            if not k:
                raise TokenError("JWK oct key senza campo 'k'.")
            return _b64url_decode(k)
        if kty == "RSA":
            try:
                from jwt.algorithms import RSAAlgorithm

                return RSAAlgorithm.from_jwk(key)
            except ImportError as exc:  # pragma: no cover - cryptography opzionale
                raise TokenError(
                    "Verifica RS256 richiede il pacchetto opzionale 'cryptography'."
                ) from exc
        raise TokenError(f"JWK kty non supportato: {kty!r}.")

    async def verify_id_token(
        self,
        token: str,
        *,
        nonce: str | None = None,
    ) -> dict[str, Any]:
        """Verify an OIDC id_token and return its claims.

        Checks signature, ``iss``, ``aud``, ``exp`` and optional ``nonce``.
        Raises :class:`app.auth.errors.TokenError` on any failure.
        """
        if not token:
            raise TokenError("id_token mancante.")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"id_token non valido: {exc}") from exc

        algorithm = header.get("alg", "")
        if algorithm not in self.settings.algorithms:
            raise TokenError(
                f"Algoritmo id_token non consentito: {algorithm!r} "
                f"(attesi: {self.settings.algorithms})."
            )

        jwks = await self.get_jwks()
        key = self._find_key(jwks, header.get("kid"))
        if key is None:
            # Key rotation: refetch once and retry before failing.
            jwks = await self.get_jwks(force=True)
            key = self._find_key(jwks, header.get("kid"))
        if key is None:
            raise TokenError("Chiave JWKS non trovata per il kid dell'id_token.")

        key_material = self._key_material(key, algorithm)
        try:
            claims = jwt.decode(
                token,
                key_material,
                algorithms=[algorithm],
                audience=self.settings.client_id,
                issuer=self.settings.issuer,
                options={
                    "verify_aud": bool(self.settings.client_id),
                    "verify_iss": bool(self.settings.issuer),
                    "verify_exp": True,
                },
            )
        except jwt.ExpiredSignatureError as exc:
            raise TokenError("id_token scaduto.") from exc
        except jwt.InvalidAudienceError as exc:
            raise TokenError("id_token audience non valida.") from exc
        except jwt.InvalidIssuerError as exc:
            raise TokenError("id_token issuer non valido.") from exc
        except jwt.InvalidTokenError as exc:
            raise TokenError(f"id_token non valido: {exc}") from exc

        if nonce is not None and claims.get("nonce") != nonce:
            raise TokenError("id_token nonce non corrispondente.")
        return claims


class FakeOIDCProvider:
    """Deterministic offline OIDC provider for tests (no network).

    Signs HS256 id_tokens with a fixed secret and exposes a matching ``oct``
    JWKS. The same instance can be passed as ``http_client`` to
    :class:`OIDCVerifier` so discovery and JWKS fetch are served in-process.
    """

    def __init__(
        self,
        *,
        issuer: str = "https://fake-oidc.example",
        client_id: str = "km-engine-test",
        secret: str = "fake-oidc-secret-0123456789abcdef",
        kid: str = "fake-oidc-kid",
    ) -> None:
        self.issuer = issuer
        self.client_id = client_id
        self.secret = secret
        self.kid = kid
        self.discovery_url = f"{issuer}/.well-known/openid-configuration"
        self.jwks_uri = f"{issuer}/jwks"

    def discovery_document(self) -> dict[str, Any]:
        return {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.issuer}/authorize",
            "token_endpoint": f"{self.issuer}/token",
            "jwks_uri": self.jwks_uri,
        }

    def jwks(self) -> dict[str, Any]:
        return {
            "keys": [
                {
                    "kty": "oct",
                    "kid": self.kid,
                    "alg": "HS256",
                    "k": base64.urlsafe_b64encode(self.secret.encode()).decode().rstrip("="),
                }
            ]
        }

    async def get(self, url: str) -> dict[str, Any]:
        if url == self.discovery_url:
            return self.discovery_document()
        if url == self.jwks_uri:
            return self.jwks()
        raise RuntimeError(f"FakeOIDCProvider: URL inatteso {url!r}")

    def issue_id_token(
        self,
        sub: str,
        *,
        nonce: str | None = None,
        aud: str | None = None,
        exp: datetime | None = None,
        extra_claims: dict[str, Any] | None = None,
    ) -> str:
        """Sign a deterministic HS256 id_token."""
        now = datetime.now(UTC)
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": sub,
            "aud": aud or self.client_id,
            "iat": now,
            "exp": exp or (now + timedelta(minutes=5)),
            "jti": uuid.uuid4().hex,
        }
        if nonce is not None:
            claims["nonce"] = nonce
        if extra_claims:
            claims.update(extra_claims)
        return jwt.encode(claims, self.secret, algorithm="HS256", headers={"kid": self.kid})


_oidc_verifier: OIDCVerifier | None = None


def get_oidc_verifier() -> OIDCVerifier:
    """Return a process-wide OIDC verifier (JWKS/discovery cache condiviso)."""
    global _oidc_verifier
    if _oidc_verifier is None:
        _oidc_verifier = OIDCVerifier(OIDCSettings())
    return _oidc_verifier


def _username_from_claims(claims: dict[str, Any]) -> str:
    return str(claims.get("preferred_username") or claims.get("sub") or "")


async def oidc_login(
    conn: psycopg.Connection,
    id_token: str,
    *,
    nonce: str | None = None,
    settings: AuthSettings | None = None,
    verifier: OIDCVerifier | None = None,
) -> Principal:
    """Verify an OIDC id_token and resolve the local Principal.

    The local user must already exist (no auto-provisioning in this iteration):
    the id_token ``preferred_username`` (fallback ``sub``) is matched against
    ``users.username``. Roles/teams are resolved from Postgres as usual.
    """
    s = settings or AuthSettings()
    v = verifier or OIDCVerifier()
    claims = await v.verify_id_token(id_token, nonce=nonce)
    username = _username_from_claims(claims)
    if not username:
        raise InvalidCredentialsError("id_token senza sub/preferred_username.")
    row = conn.execute(
        "SELECT id, active FROM users WHERE username = %s", (username,)
    ).fetchone()
    if row is None or not row[1]:
        raise InvalidCredentialsError("Credenziali non valide.")
    user_id = row[0]
    roles, teams = resolve_identity(conn, user_id)
    return Principal(
        user_id=str(user_id),
        roles=tuple(roles),
        teams=tuple(teams),
        tenant=s.tenant,
        jti=str(claims.get("jti", "")),
    )


async def oidc_issue_tokens(
    conn: psycopg.Connection,
    id_token: str,
    *,
    nonce: str | None = None,
    settings: AuthSettings | None = None,
    verifier: OIDCVerifier | None = None,
) -> dict[str, Any]:
    """Verify an OIDC id_token and issue the local access+refresh pair."""
    principal = await oidc_login(
        conn, id_token, nonce=nonce, settings=settings, verifier=verifier
    )
    return issue_token_pair(conn, principal.user_id, settings=settings)
