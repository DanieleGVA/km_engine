"""Passo 4 PROGRAMMA-UNICO: primitiva judge().

Obiettivo: un solo modo per chiedere giudizio strutturato all'LLM; output
sempre schema-valido o errore esplicito; la traduzione non cambia di un bit.
Verifiche: output non conforme -> un retry con l'errore in coda, poi
JudgeOutputError; FakeLLMClient.judge deterministico su fixture; tutti i test
esistenti di translate verdi senza modifica.
"""
from __future__ import annotations

import json

import pytest
from pydantic import BaseModel, Field

from app.domain.llm import (
    FakeLLMClient,
    HttpLLMClient,
    JudgeOutputError,
    _parse_judge,
)


class LineVerdict(BaseModel):
    """Schema di esempio per i test del giudice."""

    status: str = Field(pattern="^(ok|correct|add|delete|flag)$")
    reason: str = ""
    severity: str = Field(default="low", pattern="^(low|medium|high)$")
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class _FakeHttp:
    """HttpLLMClient con _chat sostituito (nessuna rete)."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def _chat(self, endpoint, model, api_key, system, user) -> str:
        self.calls += 1
        return self.answers.pop(0)


def _http_with(answers: list[str]) -> HttpLLMClient:
    client = HttpLLMClient.__new__(HttpLLMClient)
    client.settings = type("S", (), {"llm_endpoint": "x", "llm_model": "m", "llm_api_key": None})()
    client.judge_settings = type("J", (), {"endpoint": None, "model": None, "api_key": None})()
    client._chat = _FakeHttp(answers)._chat  # type: ignore[assignment]
    return client


def test_judge_valid_output() -> None:
    client = _http_with([json.dumps({"status": "ok", "reason": "match", "confidence": 0.9})])
    result = asyncio_run(client.judge("system", "user", LineVerdict))
    assert result["status"] == "ok"
    assert result["confidence"] == 0.9


def test_judge_retry_then_success() -> None:
    """Output non conforme -> un retry con l'errore in coda, poi successo."""
    client = _http_with([
        "not json at all",
        json.dumps({"status": "flag", "reason": "dose", "severity": "high"}),
    ])
    result = asyncio_run(client.judge("system", "user", LineVerdict))
    assert result["status"] == "flag"
    assert result["severity"] == "high"


def test_judge_retry_then_error() -> None:
    """Due output non conformi -> JudgeOutputError (mai output non validato)."""
    client = _http_with(["garbage", "still garbage"])
    with pytest.raises(JudgeOutputError):
        asyncio_run(client.judge("system", "user", LineVerdict))


def test_judge_schema_invalid_rejected() -> None:
    """Output JSON ma fuori schema (status non nell'enum) -> retry -> errore."""
    client = _http_with([
        json.dumps({"status": "boh"}),
        json.dumps({"status": "boh"}),
    ])
    with pytest.raises(JudgeOutputError):
        asyncio_run(client.judge("system", "user", LineVerdict))


def test_fake_judge_deterministic() -> None:
    fake = FakeLLMClient(judgements={("s", "u"): {"status": "ok", "confidence": 0.8}})
    r1 = asyncio_run(fake.judge("s", "u", LineVerdict))
    r2 = asyncio_run(fake.judge("s", "u", LineVerdict))
    assert r1 == r2 == {"status": "ok", "reason": "", "severity": "low", "confidence": 0.8}
    # coppia sconosciuta -> errore esplicito (mai output inventato)
    with pytest.raises(KeyError):
        asyncio_run(fake.judge("x", "y", LineVerdict))


def test_parse_judge_strips_fences() -> None:
    out = _parse_judge('```json\n{"status": "ok"}\n```', LineVerdict)
    assert out["status"] == "ok"


def test_translate_unchanged() -> None:
    """La traduzione non cambia di un bit (FakeLLMClient)."""
    fake = FakeLLMClient(translations={"ciao": "hello"})
    assert asyncio_run(fake.translate("ciao", source_lang="it", target_lang="en")) == "hello"
    assert asyncio_run(fake.translate("x", source_lang="it", target_lang="en")) == "x\n\n[untranslated]"


def asyncio_run(coro):
    import asyncio
    return asyncio.run(coro)
