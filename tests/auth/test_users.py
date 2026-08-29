"""Test gestione utenti: create/get/list/activate, ruoli e teams."""
from __future__ import annotations

import uuid

import pytest

from app.auth import (
    DuplicateUserError,
    UserNotFoundError,
    assign_role,
    assign_team,
    create_user,
    get_or_create_team,
    get_user,
    list_users,
    resolve_identity,
    revoke_role,
    revoke_team,
    set_user_active,
)


class TestCreateAndGet:
    def test_create_user_returns_fields_and_hashes_password(self, conn):
        user = create_user(
            conn, "test_alice", "test_alice@example.test", "password-alice-123",
            roles=("editor",), teams=("test_team_eng",),
        )
        assert user["username"] == "test_alice"
        assert user["active"] is True
        assert user["roles"] == ["editor"]
        assert user["teams"] == ["test_team_eng"]
        # nel DB c'e' l'hash, mai la password in chiaro
        stored = conn.execute(
            "SELECT password_hash FROM users WHERE id = %s", (user["id"],)
        ).fetchone()[0]
        assert stored.startswith("$argon2")
        assert "password-alice-123" not in stored

    def test_get_user_by_username_and_id(self, conn, make_user):
        created = make_user("bob", roles=("viewer", "editor"))
        by_name = get_user(conn, username="test_bob")
        by_id = get_user(conn, user_id=created["id"])
        assert by_name["id"] == by_id["id"] == created["id"]
        # unione permissiva: piu' ruoli risolti insieme (ADR-002 D2)
        assert sorted(by_name["roles"]) == ["editor", "viewer"]

    def test_get_missing_user_returns_none(self, conn):
        assert get_user(conn, username="test_nobody") is None
        assert get_user(conn, user_id=uuid.uuid4()) is None

    def test_duplicate_username_rejected(self, conn, make_user):
        make_user("carol")
        with pytest.raises(DuplicateUserError, match="gia' registrati"):
            create_user(conn, "test_carol", "other@example.test", "password-carol-123")
        # la transazione e' rollback-ata: nessuna riga orfana
        assert get_user(conn, username="test_carol")

    def test_duplicate_email_rejected(self, conn, make_user):
        make_user("dave")
        with pytest.raises(DuplicateUserError):
            create_user(conn, "test_dave2", "test_dave@example.test", "password-dave-123")

    def test_unknown_role_rejected_explicitly(self, conn):
        with pytest.raises(ValueError, match="Ruolo non valido"):
            create_user(
                conn, "test_eve", "test_eve@example.test", "password-eve-123",
                roles=("superuser",),
            )
        assert get_user(conn, username="test_eve") is None

    def test_short_password_rejected_at_creation(self, conn):
        with pytest.raises(ValueError, match="lunghezza minima"):
            create_user(conn, "test_frank", "test_frank@example.test", "short-pw")
        assert get_user(conn, username="test_frank") is None


class TestListAndActivate:
    def test_list_users_includes_roles_teams_and_active_filter(self, conn, make_user):
        make_user("gina", roles=("admin",), teams=("test_team_ops",))
        make_user("hank", roles=("viewer",))
        make_user("iris", roles=("viewer",))
        set_user_active(conn, get_user(conn, username="test_iris")["id"], active=False)
        all_users = {u["username"]: u for u in list_users(conn)}
        assert all_users["test_gina"]["roles"] == ["admin"]
        assert all_users["test_gina"]["teams"] == ["test_team_ops"]
        active_only = list_users(conn, active=True)
        assert "test_iris" not in {u["username"] for u in active_only}

    def test_set_user_active_deactivates_and_reactivates(self, conn, make_user):
        user = make_user("jack")
        uid = user["id"]
        set_user_active(conn, uid, active=False)
        assert get_user(conn, user_id=uid)["active"] is False
        set_user_active(conn, uid, active=True)
        assert get_user(conn, user_id=uid)["active"] is True

    def test_set_user_active_missing_user_raises(self, conn):
        with pytest.raises(UserNotFoundError):
            set_user_active(conn, uuid.uuid4(), active=False)


class TestRolesAndTeams:
    def test_assign_and_revoke_role(self, conn, make_user):
        user = make_user("kate", roles=("viewer",))
        assign_role(conn, user["id"], "editor", granted_by=user["id"])
        assert resolve_identity(conn, user["id"])[0] == ["editor", "viewer"]
        assert revoke_role(conn, user["id"], "editor") is True
        assert resolve_identity(conn, user["id"])[0] == ["viewer"]
        # idempotente: revocare di nuovo non fa nulla
        assert revoke_role(conn, user["id"], "editor") is False

    def test_assign_and_revoke_team(self, conn, make_user):
        user = make_user("liam", teams=("test_team_a",))
        assign_team(conn, user["id"], "test_team_b")
        assert resolve_identity(conn, user["id"])[1] == ["test_team_a", "test_team_b"]
        assert revoke_team(conn, user["id"], "test_team_a") is True
        assert resolve_identity(conn, user["id"])[1] == ["test_team_b"]

    def test_get_or_create_team_is_idempotent(self, conn):
        first = get_or_create_team(conn, "test_team_x", description="Team X")
        second = get_or_create_team(conn, "test_team_x")
        assert first == second
