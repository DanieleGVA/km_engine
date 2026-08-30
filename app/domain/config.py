"""Configuration for the domain LLM client (env prefix KM_)."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMSettings(BaseSettings):
    """LLM adapter settings.

    Read from ``KM_LLM_API_KEY`` / ``KM_LLM_ENDPOINT`` / ``KM_LLM_MODEL`` and,
    in dev, from the repo-root ``.env`` file. Tests never instantiate the HTTP
    client, so no network call is made.
    """

    model_config = SettingsConfigDict(
        env_prefix="KM_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    llm_api_key: str | None = None
    llm_endpoint: str | None = None
    llm_model: str | None = None


def get_llm_settings() -> LLMSettings:
    """Build settings from the environment."""
    return LLMSettings()
