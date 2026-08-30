"""LLM client protocol and implementations (WP-A2).

``LLMClient`` is the protocol used by ``translate_document``. ``HttpLLMClient``
is the real OpenAI-compatible adapter (never called in tests). ``FakeLLMClient``
is deterministic: it returns a fixture translation for known inputs and
``input + "\n\n[untranslated]"`` for unknown inputs.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import httpx

from app.domain.config import LLMSettings, get_llm_settings


@runtime_checkable
class LLMClient(Protocol):
    """Translate a text block from ``source_lang`` to ``target_lang``."""

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str
    ) -> str: ...


class HttpLLMClient:
    """OpenAI-compatible chat-completions client (httpx).

    Configure with ``KM_LLM_API_KEY`` / ``KM_LLM_ENDPOINT`` / ``KM_LLM_MODEL``.
    """

    def __init__(self, settings: LLMSettings | None = None) -> None:
        self.settings = settings or get_llm_settings()
        if not self.settings.llm_endpoint:
            raise ValueError("KM_LLM_ENDPOINT is required for HttpLLMClient")
        if not self.settings.llm_model:
            raise ValueError("KM_LLM_MODEL is required for HttpLLMClient")

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str
    ) -> str:
        headers = {"Content-Type": "application/json"}
        if self.settings.llm_api_key:
            headers["Authorization"] = f"Bearer {self.settings.llm_api_key}"
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a faithful culinary translator. Translate the "
                        "document from "
                        f"{source_lang} to {target_lang}. Preserve every "
                        "{Nk} placeholder exactly, in the same order, and do "
                        "not invent, drop or reorder numbers."
                    ),
                },
                {"role": "user", "content": text},
            ],
            "temperature": 0,
        }
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.post(
                self.settings.llm_endpoint, json=payload, headers=headers
            )
            response.raise_for_status()
            data = response.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"unexpected LLM response shape: {data!r}") from exc


class FakeLLMClient:
    """Deterministic LLM client for tests (no network).

    ``translations`` maps an exact input text to its translated output. Any
    input not present in the mapping is returned unchanged plus an explicit
    ``[untranslated]`` note.
    """

    def __init__(self, translations: dict[str, str] | None = None) -> None:
        self.translations = dict(translations or {})

    async def translate(
        self, text: str, *, source_lang: str, target_lang: str
    ) -> str:
        if text in self.translations:
            return self.translations[text]
        return text + "\n\n[untranslated]"
