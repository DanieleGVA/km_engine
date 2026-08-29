"""Configurazione del layer auth (env con prefisso KM_, vedi .env.example)."""
from __future__ import annotations

from datetime import timedelta

from pydantic_settings import BaseSettings, SettingsConfigDict


class AuthSettings(BaseSettings):
    """Impostazioni auth: DSN Postgres, secret JWT, TTL dei token, bootstrap admin.

    Lette da variabili d'ambiente con prefisso ``KM_`` (es. ``KM_PG_DSN``) e,
    in dev, dal file ``.env`` della repo root.
    """

    model_config = SettingsConfigDict(
        env_prefix="KM_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    pg_dsn: str = "postgresql://km:km_dev_password@localhost:5432/km_engine"
    jwt_secret: str = "dev-only-secret-change-in-prod"
    jwt_algorithm: str = "HS256"  # migrazione a RS256 con OIDC (ADR-002 D1/D7)
    access_token_ttl: timedelta = timedelta(minutes=15)
    refresh_token_ttl: timedelta = timedelta(days=14)
    tenant: str = "default"  # tenant unico nel MVP (ADR-002 D3)
    admin_username: str = "admin"
    admin_password: str = ""


def get_auth_settings() -> AuthSettings:
    """Costruisce le impostazioni dall'ambiente (una richiesta HTTP = una lettura)."""
    return AuthSettings()
