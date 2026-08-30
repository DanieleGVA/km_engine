"""Test OIDC id_token verification and login (WP-E1, GE1)."""
from __future__ import annotations

import pytest

from app.auth import (
    FakeOIDCProvider,
    InvalidCredentialsError,
    OIDCSettings,
    OIDCVerifier,
    TokenError,
    oidc_issue_tokens,
    oidc_login,
)


@pytest.fixture
def provider() -> FakeOIDCProvider:
    return FakeOIDCProvider()


@pytest.fixture
def verifier(provider: FakeOIDCProvider) -> OIDCVerifier:
    return OIDCVerifier(
        OIDCSettings(
            issuer=provider.issuer,
            client_id=provider.client_id,
            discovery_url=provider.discovery_url,
            algorithms=["HS256"],
        ),
        http_client=provider,
    )


class TestOIDCVerifier:
    async def test_verify_valid_id_token(self, provider, verifier) -> None:
        token = provider.issue_id_token("user-123", nonce="nonce-abc")
        claims = await verifier.verify_id_token(token, nonce="nonce-abc")
        assert claims["sub"] == "user-123"
        assert claims["iss"] == provider.issuer
        assert claims["aud"] == provider.client_id
        assert claims["nonce"] == "nonce-abc"

    async def test_verify_rejects_tampered_token(self, provider, verifier) -> None:
        token = provider.issue_id_token("user-123")
        tampered = token[:-4] + ("AAAA" if token[-4:] != "AAAA" else "BBBB")
        with pytest.raises(TokenError):
            await verifier.verify_id_token(tampered)

    async def test_verify_rejects_wrong_nonce(self, provider, verifier) -> None:
        token = provider.issue_id_token("user-123", nonce="nonce-abc")
        with pytest.raises(TokenError):
            await verifier.verify_id_token(token, nonce="nonce-other")

    async def test_verify_rejects_wrong_audience(self, provider) -> None:
        verifier = OIDCVerifier(
            OIDCSettings(
                issuer=provider.issuer,
                client_id="another-client",
                discovery_url=provider.discovery_url,
                algorithms=["HS256"],
            ),
            http_client=provider,
        )
        token = provider.issue_id_token("user-123")
        with pytest.raises(TokenError):
            await verifier.verify_id_token(token)


class TestOIDCLogin:
    async def test_oidc_login_returns_principal(
        self, conn, make_user, provider, verifier
    ) -> None:
        make_user("oidcuser", roles=("viewer",), teams=("team-a",))
        token = provider.issue_id_token(
            "test_oidcuser", extra_claims={"preferred_username": "test_oidcuser"}
        )
        principal = await oidc_login(conn, token, verifier=verifier)
        assert principal.user_id
        assert principal.roles == ("viewer",)
        assert principal.teams == ("team-a",)
        assert principal.tenant == "default"

    async def test_oidc_login_rejects_unknown_user(
        self, conn, provider, verifier
    ) -> None:
        token = provider.issue_id_token("test_oidc_unknown")
        with pytest.raises(InvalidCredentialsError):
            await oidc_login(conn, token, verifier=verifier)

    async def test_oidc_issue_tokens(self, conn, make_user, provider, verifier) -> None:
        make_user("oidcuser2", roles=("viewer",))
        token = provider.issue_id_token(
            "test_oidcuser2", extra_claims={"preferred_username": "test_oidcuser2"}
        )
        result = await oidc_issue_tokens(conn, token, verifier=verifier)
        assert result["access_token"]
        assert result["refresh_token"]
        assert result["token_type"] == "bearer"
        assert result["roles"] == ["viewer"]
