"""Test JWT: login, scadenza, refresh con rotazione, riuso -> revoca, logout."""
from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.auth import (
    InactiveUserError,
    InvalidCredentialsError,
    TokenError,
    TokenExpiredError,
    TokenReuseError,
    decode_token,
    issue_access_token,
    list_refresh_tokens,
    login,
    logout,
    refresh,
    revoke_all_user_tokens,
    set_user_active,
)
from app.auth import tokens as tokens_mod
from app.auth.config import AuthSettings


class TestLogin:
    def test_login_returns_token_pair_with_expected_claims(self, conn, make_user, settings):
        make_user("anna", roles=("editor",), teams=("test_team_eng",))
        result = login(conn, "test_anna", "test-password-123", settings=settings)
        claims = decode_token(result["access_token"], settings=settings)
        user_id = get_user_id(conn, "test_anna")
        assert claims["sub"] == str(user_id)
        assert claims["typ"] == "access"
        assert claims["roles"] == ["editor"]
        assert claims["teams"] == ["test_team_eng"]
        assert claims["tenant"] == "default"
        assert claims["jti"]
        assert claims["exp"] - claims["iat"] == 15 * 60  # 15 minuti (ADR-002 D1)
        assert result["expires_in"] == 900
        # il refresh token ha typ refresh e l'hash finisce in tabella, il chiaro no
        rclaims = decode_token(result["refresh_token"], settings=settings, expected_type="refresh")
        assert rclaims["sub"] == str(user_id)
        assert rclaims["exp"] - rclaims["iat"] == 14 * 24 * 3600  # 14 giorni
        rows = list_refresh_tokens(conn, user_id)
        assert len(rows) == 1
        assert rows[0]["revoked_at"] is None
        assert result["refresh_token"] not in rows[0]["token_hash"]

    def test_login_wrong_password_fails(self, conn, make_user, settings):
        make_user("bruno")
        with pytest.raises(InvalidCredentialsError):
            login(conn, "test_bruno", "wrong-password-999", settings=settings)

    def test_login_unknown_user_fails(self, conn, settings):
        with pytest.raises(InvalidCredentialsError):
            login(conn, "test_ghost", "whatever-password", settings=settings)

    def test_login_inactive_user_same_error_as_wrong_password(self, conn, make_user, settings):
        user = make_user("cesare")
        set_user_active(conn, user["id"], active=False)
        # stesso errore: il login non rivela lo stato dell'account
        with pytest.raises(InvalidCredentialsError):
            login(conn, "test_cesare", "test-password-123", settings=settings)


class TestAccessTokenValidation:
    def test_expired_access_token_rejected(self, settings):
        token = issue_access_token(
            uuid.uuid4(), roles=["viewer"], teams=[], settings=settings,
            ttl=timedelta(minutes=-1),
        )
        with pytest.raises(TokenExpiredError, match="scaduto"):
            decode_token(token, settings=settings)

    def test_wrong_signature_rejected(self, settings):
        token = issue_access_token(uuid.uuid4(), roles=["viewer"], teams=[], settings=settings)
        other = AuthSettings(jwt_secret="another-secret-0123456789abcdef0")
        with pytest.raises(TokenError):
            decode_token(token, settings=other)

    def test_refresh_token_not_usable_as_access(self, conn, make_user, settings):
        make_user("dario")
        result = login(conn, "test_dario", "test-password-123", settings=settings)
        with pytest.raises(TokenError, match="Tipo di token errato"):
            decode_token(result["refresh_token"], settings=settings, expected_type="access")


class TestRefreshRotation:
    def test_refresh_rotates_old_revoked_new_valid(self, conn, make_user, settings):
        make_user("elena")
        first = login(conn, "test_elena", "test-password-123", settings=settings)
        second = refresh(conn, first["refresh_token"], settings=settings)
        assert second["refresh_token"] != first["refresh_token"]
        assert second["access_token"] != first["access_token"]
        # il vecchio e' revocato, il nuovo attivo
        rows = list_refresh_tokens(conn, get_user_id(conn, "test_elena"))
        active = [r for r in rows if r["revoked_at"] is None]
        assert len(active) == 1
        assert active[0]["token_hash"] == tokens_mod._hash_token(second["refresh_token"])

    def test_reuse_of_rotated_refresh_revokes_whole_session(self, conn, make_user, settings):
        make_user("fabio")
        first = login(conn, "test_fabio", "test-password-123", settings=settings)
        second = refresh(conn, first["refresh_token"], settings=settings)
        third = refresh(conn, second["refresh_token"], settings=settings)
        # riuso del primo (gia' ruotato): possibile furto -> revoca a cascata
        with pytest.raises(TokenReuseError, match="revocat"):
            refresh(conn, first["refresh_token"], settings=settings)
        # a cascata: anche il refresh piu' recente ora e' revocato
        with pytest.raises(TokenReuseError):
            refresh(conn, third["refresh_token"], settings=settings)
        assert list_refresh_tokens(conn, get_user_id(conn, "test_fabio")) and all(
            r["revoked_at"] is not None
            for r in list_refresh_tokens(conn, get_user_id(conn, "test_fabio"))
        )

    def test_expired_refresh_token_rejected(self, conn, make_user, settings):
        make_user("gino")
        result = login(conn, "test_gino", "test-password-123", settings=settings)
        expired = _force_expired_refresh(conn, result["refresh_token"], settings)
        with pytest.raises(TokenExpiredError):
            refresh(conn, expired, settings=settings)

    def test_refresh_of_inactive_user_revokes_sessions(self, conn, make_user, settings):
        user = make_user("hugo")
        session = login(conn, "test_hugo", "test-password-123", settings=settings)
        # disattivazione "nuda" (bypass della revoca automatica) per esercitare
        # il ramo InactiveUserError di tokens.refresh: rinnovo vietato + revoca
        conn.execute("UPDATE users SET active = FALSE WHERE id = %s", (user["id"],))
        with pytest.raises(InactiveUserError):
            refresh(conn, session["refresh_token"], settings=settings)
        rows = list_refresh_tokens(conn, user["id"])
        assert rows and all(r["revoked_at"] is not None for r in rows)


class TestLogoutAndRevocation:
    def test_logout_revokes_refresh_token(self, conn, make_user, settings):
        make_user("ilaria")
        result = login(conn, "test_ilaria", "test-password-123", settings=settings)
        assert logout(conn, result["refresh_token"]) is True
        with pytest.raises(TokenReuseError):
            refresh(conn, result["refresh_token"], settings=settings)
        # logout ripetuto: non era piu' attivo
        assert logout(conn, result["refresh_token"]) is False

    def test_revoke_all_user_tokens_logout_everywhere(self, conn, make_user, settings):
        user = make_user("luca")
        s1 = login(conn, "test_luca", "test-password-123", settings=settings)
        s2 = login(conn, "test_luca", "test-password-123", settings=settings)
        assert revoke_all_user_tokens(conn, user["id"]) == 2
        for tok in (s1["refresh_token"], s2["refresh_token"]):
            with pytest.raises(TokenReuseError):
                refresh(conn, tok, settings=settings)

    def test_deactivation_revokes_active_refresh_tokens(self, conn, make_user, settings):
        user = make_user("marta")
        session = login(conn, "test_marta", "test-password-123", settings=settings)
        set_user_active(conn, user["id"], active=False)
        rows = list_refresh_tokens(conn, user["id"])
        assert rows and all(r["revoked_at"] is not None for r in rows)
        with pytest.raises((TokenReuseError, InactiveUserError)):
            refresh(conn, session["refresh_token"], settings=settings)


def get_user_id(conn, username: str):
    row = conn.execute("SELECT id FROM users WHERE username = %s", (username,)).fetchone()
    assert row is not None, f"utente {username} non trovato"
    return row[0]


def _force_expired_refresh(conn, refresh_token: str, settings: AuthSettings) -> str:
    """Riemette lo stesso refresh token con exp nel passato (per test)."""
    import jwt as pyjwt

    claims = pyjwt.decode(refresh_token, settings.jwt_secret, algorithms=["HS256"])
    claims["exp"] -= 20 * 24 * 3600  # 20 giorni nel passato
    expired = pyjwt.encode(claims, settings.jwt_secret, algorithm="HS256")
    row = conn.execute(
        "UPDATE refresh_tokens SET token_hash = %s, expires_at = now() - interval '20 days'"
        " WHERE token_hash = %s RETURNING id",
        (tokens_mod._hash_token(expired), tokens_mod._hash_token(refresh_token)),
    ).fetchone()
    assert row is not None
    return expired
