"""LLM client protocol and implementations (WP-A2 + passo 4 PROGRAMMA-UNICO).

``LLMClient`` is the protocol used by ``translate_document`` and by the
``judge()`` primitive (Fase 0/1). ``HttpLLMClient`` is the real
OpenAI-compatible adapter (never called in tests). ``FakeLLMClient`` is
deterministic: it returns fixture translations/judgements for known inputs.

``judge(system, user, schema)``: un solo modo per chiedere giudizio
strutturato all'LLM. Output sempre schema-valido o errore esplicito
(:class:`JudgeOutputError`); un retry con l'errore in coda, poi errore.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Sequence
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ValidationError

from app.domain.config import (
    JudgeSettings,
    LLMSettings,
    get_judge_settings,
    get_llm_settings,
)
from app.domain.errors import DomainError


class JudgeOutputError(DomainError):
    """Raised when the LLM judge output is not schema-valid after one retry."""


def build_translation_system_prompt(
    source_lang: str,
    target_lang: str,
    glossary: Sequence[str] | None = None,
    *,
    ingredient_section: str = "Ingredients",
    method_section: str = "Method",
) -> str:
    """Prompt di sistema della traduzione (WP-F6): P2 + struttura + glossario.

    Le intestazioni di sezione vanno dichiarate: un modello lasciato libero
    traduce ``## Procedimento`` in ``## Procedure`` — corretto come inglese,
    illeggibile per il parser, che cerca ``## Method``. Il documento tradotto
    e' un formato, non prosa.

    E' una funzione pura cosi' il manifest del golden puo' registrarne
    l'hash: se il prompt cambia, il golden va rigenerato e si sa perche'.
    """
    lines = [
        (
            "You are a faithful culinary translator. Translate the document "
            f"from {source_lang} to {target_lang}."
        ),
        (
            "Preserve every {Nk} placeholder exactly, in the same order, and "
            "do not invent, drop or reorder numbers."
        ),
        "Do not translate, reformat or comment on the {Nk} placeholders.",
        "",
        (
            "Reply with the translated document only: no preamble, no "
            "comment, no code fence. Keep this exact structure:"
        ),
        "- first line: the translated title, nothing else;",
        f"- a line reading exactly '## {ingredient_section}';",
        (
            "- one ingredient per line, starting with '- ', in the same "
            "order as the source;"
        ),
        f"- a line reading exactly '## {method_section}';",
        (
            "- the steps, numbered '1. ', '2. ', ... in the same order as "
            "the source, one per line."
        ),
        "",
    ]
    if glossary:
        # Nessun taglio: tagliare un elenco ordinato alfabeticamente
        # toglierebbe termini a caso, e il termine mancante sarebbe proprio
        # quello che il traduttore parafrasa. Il prompt cresce col glossario,
        # ma il golden si genera una volta sola.
        terms = list(dict.fromkeys(glossary))
        lines.append(
            "Use exactly these canonical terms for the ingredients whenever "
            "the source mentions them; do not substitute a synonym:"
        )
        lines.extend(f"- {term}" for term in terms)
    return "\n".join(lines)


def translation_prompt_sha256(
    source_lang: str,
    target_lang: str,
    glossary: Sequence[str] | None = None,
    **kwargs: str,
) -> str:
    """Hash del prompt, per il manifest del golden."""
    return hashlib.sha256(
        build_translation_system_prompt(
            source_lang, target_lang, glossary, **kwargs
        ).encode()
    ).hexdigest()


@runtime_checkable
class LLMClient(Protocol):
    """Translate a text block or produce a structured judgement."""

    async def translate(
        self,
        text: str,
        *,
        source_lang: str,
        target_lang: str,
        glossary: Sequence[str] | None = None,
    ) -> str: ...

    async def judge(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> dict[str, Any]: ...


def _judge_prompt(system: str, user: str, schema: type[BaseModel]) -> str:
    return (
        f"{user}\n\n"
        "Respond with a single JSON object that validates against this JSON "
        "schema. No prose, no markdown fences, no trailing text.\n"
        f"JSON schema:\n{json.dumps(schema.model_json_schema())}"
    )


def _parse_judge(text: str, schema: type[BaseModel]) -> dict[str, Any]:
    """Parse + validate the judge output; raises on non-conformant output.

    ``model_dump(by_alias=True)``: il JSON usa gli alias (es. ``class``),
    cosi' il chiamante puo' ri-validare con lo stesso schema.
    """
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json")
    data = json.loads(text)
    return schema.model_validate(data).model_dump(by_alias=True)


class HttpLLMClient:
    """OpenAI-compatible chat-completions client (httpx).

    Configure with ``KM_LLM_API_KEY`` / ``KM_LLM_ENDPOINT`` / ``KM_LLM_MODEL``.
    Il giudice usa ``KM_JUDGE_*`` (modello separato), con fallback sul modello
    di traduzione.
    """

    def __init__(
        self,
        settings: LLMSettings | None = None,
        judge_settings: JudgeSettings | None = None,
    ) -> None:
        self.settings = settings or get_llm_settings()
        self.judge_settings = judge_settings or get_judge_settings()
        if not self.settings.llm_endpoint:
            raise ValueError("KM_LLM_ENDPOINT is required for HttpLLMClient")
        if not self.settings.llm_model:
            raise ValueError("KM_LLM_MODEL is required for HttpLLMClient")

    def _headers(self, api_key: str | None) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _chat(
        self, endpoint: str, model: str, api_key: str | None,
        system: str, user: str,
    ) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
        }
        # Retry su errori transitori (5xx, connessione): un 502 del proxy
        # LLM non deve uccidere un batch da migliaia di chiamate.
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                async with httpx.AsyncClient(timeout=self.settings.llm_timeout) as client:
                    response = await client.post(
                        endpoint, json=payload, headers=self._headers(api_key)
                    )
                    response.raise_for_status()
                    data = response.json()
                try:
                    return data["choices"][0]["message"]["content"]
                except (KeyError, IndexError, TypeError) as exc:
                    raise RuntimeError(
                        f"unexpected LLM response shape: {data!r}"
                    ) from exc
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                last_error = exc
                if attempt < 3:
                    await asyncio.sleep(5 * (2 ** attempt))
        raise last_error

    async def translate(
        self,
        text: str,
        *,
        source_lang: str,
        target_lang: str,
        glossary: Sequence[str] | None = None,
    ) -> str:
        """Traduzione vincolata al glossario (WP-F6).

        Senza il vincolo il traduttore sceglie ogni volta un sinonimo diverso
        (``EVOO``, ``olive oil``, ``extra-virgin olive oil``) e lo stadio 2
        non puo' risolverli: la copertura crolla fra sorgente e tradotto per
        una ragione che non ha nulla a che vedere col glossario. Passare le
        etichette canoniche costa poco e toglie il problema alla radice.
        """
        system = build_translation_system_prompt(
            source_lang, target_lang, glossary
        )
        return await self._chat(
            self.settings.llm_endpoint, self.settings.llm_model,
            self.settings.llm_api_key, system, text,
        )

    async def judge(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> dict[str, Any]:
        """Giudizio strutturato: JSON-mode, temperatura 0, un retry, poi errore."""
        endpoint = self.judge_settings.endpoint or self.settings.llm_endpoint
        model = self.judge_settings.model or self.settings.llm_model
        api_key = self.judge_settings.api_key or self.settings.llm_api_key
        prompt = _judge_prompt(system, user, schema)
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                text = await self._chat(endpoint, model, api_key, system, prompt)
                return _parse_judge(text, schema)
            except (json.JSONDecodeError, ValidationError, RuntimeError) as exc:
                last_error = exc
                prompt = (
                    f"Your previous answer was not valid JSON for the schema. "
                    f"Error: {exc}. Respond again with a single valid JSON "
                    f"object only.\nJSON schema:\n"
                    f"{json.dumps(schema.model_json_schema())}"
                )
        raise JudgeOutputError(f"judge output not schema-valid after retry: {last_error}")


class FakeLLMClient:
    """Deterministic LLM client for tests (no network).

    ``translations`` maps an exact input text to its translated output.
    ``judgements`` maps a ``(system, user)`` pair to a fixture dict; any
    unknown pair returns a schema-valid empty dict (all defaults).
    """

    def __init__(
        self,
        translations: dict[str, str] | None = None,
        judgements: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> None:
        self.translations = dict(translations or {})
        self.judgements = dict(judgements or {})

    async def translate(
        self,
        text: str,
        *,
        source_lang: str,
        target_lang: str,
        glossary: Sequence[str] | None = None,
    ) -> str:
        if text in self.translations:
            return self.translations[text]
        return text + "\n\n[untranslated]"

    async def judge(
        self, system: str, user: str, schema: type[BaseModel]
    ) -> dict[str, Any]:
        fixture = self.judgements.get((system, user))
        if fixture is None:
            raise KeyError(
                "no judge fixture for (system, user) pair; provide one in "
                "FakeLLMClient(judgements=...)"
            )
        return schema.model_validate(fixture).model_dump()
