"""Configuration for the WP4 ingestion pipeline (env prefix KM_)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class IngestSettings(BaseSettings):
    """Ingestion settings.

    Read from KM_* environment variables and, in dev, from the repo-root
    ``.env`` file. The LLM adapter fields are intentionally optional: the
    deterministic stub never reads them, and the LLM skeleton only documents
    where they would be used.
    """

    model_config = SettingsConfigDict(
        env_prefix="KM_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    pg_dsn: str = "postgresql://km:km_dev_password@localhost:5432/km_engine"
    chunk_size: int = 20
    cache_dir: Path = Path(".km_ingest_cache")

    # LLM adapter skeleton (not called by the deterministic stub or tests).
    llm_api_key: str | None = None
    llm_endpoint: str | None = None
    llm_model: str | None = None


def get_ingest_settings() -> IngestSettings:
    """Build settings from the environment."""
    return IngestSettings()
