"""Test bootstrap admin idempotente da KM_ADMIN_USERNAME/KM_ADMIN_PASSWORD."""
from __future__ import annotations

import pytest

from app.auth import bootstrap_admin, get_user, login


class TestBootstrapAdmin:
    def test_first_run_creates_active_admin_with_admin_role(self, conn, settings):
        result = bootstrap_admin(conn, settings)
        assert result["created"] is True
        admin = get_user(conn, username=settings.admin_username)
        assert admin is not None
        assert admin["active"] is True
        assert admin["roles"] == ["admin"]
        assert login(conn, settings.admin_username, settings.admin_password, settings=settings)

    def test_second_run_is_idempotent_no_password_change(self, conn, settings):
        first = bootstrap_admin(conn, settings)
        stored_hash = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s", (first["user_id"],),
        ).fetchone()[0]
        second = bootstrap_admin(conn, settings)
        assert second["created"] is False
        assert second["repaired"] is False
        assert second["user_id"] == first["user_id"]
        # la password non viene MAI toccata dalle riesecuzioni
        assert conn.execute(
            "SELECT password_hash FROM users WHERE id = %s", (first["user_id"],),
        ).fetchone()[0] == stored_hash
        # nessun ruolo duplicato
        assert get_user(conn, username=settings.admin_username)["roles"] == ["admin"]

    def test_repairs_missing_admin_role_and_inactive_user(self, conn, settings):
        from app.auth import revoke_role, set_user_active
        first = bootstrap_admin(conn, settings)
        revoke_role(conn, first["user_id"], "admin")
        set_user_active(conn, first["user_id"], active=False)
        result = bootstrap_admin(conn, settings)
        assert result["created"] is False
        assert result["repaired"] is True
        admin = get_user(conn, user_id=first["user_id"])
        assert admin["active"] is True
        assert admin["roles"] == ["admin"]
        assert login(conn, settings.admin_username, settings.admin_password, settings=settings)

    def test_missing_admin_password_raises_explicit(self, conn, settings):
        from app.auth.config import AuthSettings
        bad = AuthSettings(
            pg_dsn=settings.pg_dsn, jwt_secret=settings.jwt_secret,
            admin_username="test_bootstrap_bad", admin_password="",
        )
        with pytest.raises(ValueError, match="KM_ADMIN"):
            bootstrap_admin(conn, bad)
        assert get_user(conn, username="test_bootstrap_bad") is None

    def test_weak_admin_password_rejected(self, conn, settings):
        from app.auth.config import AuthSettings
        weak = AuthSettings(
            pg_dsn=settings.pg_dsn, jwt_secret=settings.jwt_secret,
            admin_username="test_bootstrap_weak", admin_password="short",
        )
        with pytest.raises(ValueError, match="lunghezza minima"):
            bootstrap_admin(conn, weak)
        assert get_user(conn, username="test_bootstrap_weak") is None
