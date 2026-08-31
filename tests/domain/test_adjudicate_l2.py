"""Passo 12 PROGRAMMA-UNICO: giudice semantico sulle escalation L2.

Obiettivo: la coda umana riceve solo divergenze reali con suggerimento
azionabile; i falsi allarmi chiusi con motivazione; i corrotti sintetici
ancora intercettati.
"""
from __future__ import annotations

import asyncio

from app.domain.adjudicate import (
    L2_SYSTEM_PROMPT,
    adjudicate_l2,
    build_l2_prompt,
)
from app.domain.llm import FakeLLMClient


def _run(coro):
    return asyncio.run(coro)


def _fake(verdict: dict, section="steps", src="src", trad="trad") -> FakeLLMClient:
    return FakeLLMClient(judgements={
        (L2_SYSTEM_PROMPT, build_l2_prompt(section, src, trad)): verdict,
    })


def test_false_alarm_closed_with_motivation() -> None:
    """Falso allarme: overall ok, motivazione registrata, non va all'umano."""
    fake = _fake({
        "overall": "ok",
        "motivation": "sinonimi e riordino, significato preservato",
        "lines": [{"line": 1, "verdict": "ok", "motivation": "sinonimo"}],
    })
    v = _run(adjudicate_l2(fake, "steps", "src", "trad"))
    assert v.overall == "ok"
    assert v.motivation
    assert not v.needs_human


def test_real_divergence_goes_to_human_with_suggestion() -> None:
    """Divergenza reale: va all'umano, suggestion mai vuota."""
    fake = _fake({
        "overall": "divergent",
        "motivation": "ingrediente aggiunto",
        "lines": [{"line": 2, "verdict": "divergent",
                  "motivation": "aggiunto 'butter'",
                  "suggestion": "rimuovere 'butter' dalla traduzione"}],
    }, section="ingredients")
    v = _run(adjudicate_l2(fake, "ingredients", "src", "trad"))
    assert v.needs_human
    assert v.lines[0].suggestion


def test_unsure_goes_to_human() -> None:
    fake = _fake({
        "overall": "unsure", "motivation": "ambiguita'",
        "lines": [{"line": 1, "verdict": "unsure", "motivation": "?"}],
    })
    v = _run(adjudicate_l2(fake, "steps", "src", "trad"))
    assert v.needs_human


def test_prompt_contains_sections() -> None:
    p = build_l2_prompt("steps", "SORGENTE", "TRADOTTO")
    assert "steps" in p and "SORGENTE" in p and "TRADOTTO" in p


def test_synthetic_corruption_still_intercepted() -> None:
    """I corrotti sintetici L1/L2 restano intercettati (nessuna regressione)."""
    from app.domain import load_domain_pack, parse_source_md, verify_l2
    from tests.domain.conftest import PACK_DIR, read_corpus
    from tests.domain.test_verify_l2 import build_translated_parsed

    pack = load_domain_pack(str(PACK_DIR))
    corpus = read_corpus()
    source = parse_source_md(
        corpus["ric-101-asparagi-burro.md"], known_units=pack.known_units())
    # corruzione: passo riscritto
    rewritten = build_translated_parsed(
        pack, source,
        steps=["Preheat the oven to 200 degrees.", "Bake for 45 minutes."])
    report = verify_l2(source, rewritten, pack=pack)
    assert not report.passed
    assert any(s.divergent for s in report.sections)
