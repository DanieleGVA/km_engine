"""Fixture condivise: dev Postgres, settings con secret di test, cleanup mirato.

Non si tocca lo schema: i test creano solo righe con prefisso test_* e le
rimuovono (audit_log compreso) alla fine di ogni test. Gli user_roles/
user_teams/refresh_tokens seguono gli utenti via ON DELETE CASCADE.
"""
from __future__ import annotations

import os

import psycopg
import pytest

from app.auth import create_user
from app.auth.config import AuthSettings

TEST_DSN = os.environ.get(
    "KM_TEST_PG_DSN",
    os.environ.get("KM_PG_DSN", "postgresql://km:km_dev_password@localhost:5432/km_engine"),
)

TEST_PASSWORD = "test-password-123"  # >= 12 caratteri (politica ADR-002 D5)


def cleanup_test_data(conn: psycopg.Connection) -> None:
    """Rimuove SOLO le righe create dai test (utenti/team/audit prefissati test_)."""
    with conn.transaction():
        rows = conn.execute(
            "SELECT id FROM users WHERE username LIKE 'test\\_%'"
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            conn.execute(
                "DELETE FROM audit_log WHERE user_id = ANY(%s) OR entity_id = ANY(%s)",
                (ids, [str(i) for i in ids]),
            )
            conn.execute("DELETE FROM users WHERE id = ANY(%s)", (ids,))
        conn.execute("DELETE FROM teams WHERE name LIKE 'test\\_%'")


@pytest.fixture()
def settings() -> AuthSettings:
    return AuthSettings(
        pg_dsn=TEST_DSN,
        jwt_secret="test-jwt-secret-0123456789abcdef",  # >= 32 byte (RFC 7518 3.2)
        admin_username="test_bootstrap_admin",
        admin_password="test-admin-password-123",
    )


@pytest.fixture()
def conn(settings: AuthSettings):
    c = psycopg.connect(settings.pg_dsn, autocommit=True)
    try:
        yield c
    finally:
        cleanup_test_data(c)
        c.close()


@pytest.fixture()
def make_user(conn: psycopg.Connection):
    """Factory: crea un utente di test con ruoli/teams e ritorna il dict di create_user."""

    def _make(
        username: str,
        password: str = TEST_PASSWORD,
        roles: tuple[str, ...] = ("viewer",),
        teams: tuple[str, ...] = (),
        email: str | None = None,
    ) -> dict:
        return create_user(
            conn,
            f"test_{username}",
            email or f"test_{username}@example.test",
            password,
            roles=roles,
            teams=teams,
        )

    return _make
