"""Passo 13 PROGRAMMA-UNICO: giudice di canone per componente + critico.

Obiettivo: ogni verdetto e' citato o e' un'astensione (CANON_GAP); gli
adattamenti dichiarati non vengono mai bocciati; nessun numero proposto tocca
un documento senza rivalidazione deterministica (P8/P9).
"""
from __future__ import annotations

import asyncio

from app.domain import load_domain_pack
from app.domain.canon_judge import (
    CANON_JUDGE_SYSTEM,
    CRITIC_SYSTEM,
    ComponentVerdict,
    LineVerdict,
    build_component_prompt,
    critic_revise,
    judge_component,
    judge_recomposition,
    validate_proposed_quantities,
    verify_citations,
)
from app.domain.llm import FakeLLMClient
from tests.domain.conftest import PACK_DIR


def _run(coro):
    return asyncio.run(coro)


def _fake(verdict: dict, component="main", card_lines=None, candidates=None) -> FakeLLMClient:
    card_lines = card_lines or ["- 100 g chicken"]
    candidates = candidates or [{"document_id": "bk_x-0001", "title": "X", "lines": ["- 100 g chicken"]}]
    return FakeLLMClient(judgements={
        (CANON_JUDGE_SYSTEM, build_component_prompt(component, card_lines, candidates)): verdict,
    })


def _judge(fake, component="main", card_lines=None, candidates=None):
    card_lines = card_lines or ["- 100 g chicken"]
    candidates = candidates or [{"document_id": "bk_x-0001", "title": "X", "lines": ["- 100 g chicken"]}]
    return _run(judge_component(fake, component, card_lines, candidates))


def test_judge_component_ok() -> None:
    fake = _fake({
        "component": "main", "overall": "ok", "confidence": 0.9,
        "motivation": "coerente col candidato",
        "lines": [{"line": 0, "verdict": "ok", "reason": "stessa dose",
                  "citation": "bk_x-0001:0", "base": "ratio"}],
    })
    v = _run(judge_component(fake, "main", ["- 100 g chicken"],
                             [{"document_id": "bk_x-0001", "title": "X", "lines": ["- 100 g chicken"]}]))
    assert v.overall == "ok"
    assert v.lines[0].citation == "bk_x-0001:0"


def test_canon_gap_when_no_referent() -> None:
    """Caso senza referente seminato => CANON_GAP (astensione, non invenzione)."""
    cands = [{"document_id": "bk_y-0001", "title": "Y", "lines": ["- 200 g z"]}]
    fake = _fake({
        "component": "main", "overall": "canon_gap", "confidence": 0.4,
        "motivation": "nessun candidato copre questo piatto",
        "lines": [],
    }, card_lines=["- 100 g x"], candidates=cands)
    v = _judge(fake, "main", ["- 100 g x"], cands)
    assert v.overall == "canon_gap"


def test_declared_adaptation_ok() -> None:
    """Card NSA con dolcificante 1:1 => ok con base declared_adaptation."""
    cands = [{"document_id": "bk_x-0001", "title": "X", "lines": ["- 100 g sugar"]}]
    fake = _fake({
        "component": "main", "overall": "ok", "confidence": 0.95,
        "motivation": "adattamento dichiarato NSA",
        "lines": [{"line": 0, "verdict": "ok", "reason": "NSA 1:1",
                  "base": "declared_adaptation"}],
    }, card_lines=["- 100 g sugar substitute"], candidates=cands)
    v = _judge(fake, "main", ["- 100 g sugar substitute"], cands)
    assert v.lines[0].base == "declared_adaptation"
    assert v.lines[0].verdict == "ok"


def test_citation_to_non_candidate_fails() -> None:
    """Una citazione a un documento non tra i candidati fa fallire il verdetto."""
    v = ComponentVerdict(
        component="main", overall="ok", confidence=0.9,
        lines=[LineVerdict(line=0, verdict="ok", reason="r",
                           citation="bk_ghost-0001:0")],
    )
    problems = verify_citations(v, {"bk_x-0001"})
    assert problems  # citazione a bk_ghost non tra i candidati


def test_unparsable_proposal_rejected() -> None:
    """Una quantita' proposta non parsabile viene respinta, mai applicata."""
    pack = load_domain_pack(str(PACK_DIR))
    v = ComponentVerdict(
        component="main", overall="correct", confidence=0.8,
        lines=[
            LineVerdict(line=0, verdict="correct", proposal="100 blob", reason="r"),
            LineVerdict(line=1, verdict="correct", proposal="100 g", reason="r"),
        ],
    )
    problems = validate_proposed_quantities(v, pack)
    assert any("blob" in p for p in problems)
    assert not any("line 1" in p for p in problems)


def test_critic_revise() -> None:
    """Genera-critica-rivedi: il critico produce un verdetto rivisto."""
    v = ComponentVerdict(component="main", overall="ok", confidence=0.9,
                         lines=[LineVerdict(line=0, verdict="ok", reason="r")])
    fake = FakeLLMClient(judgements={
        (CRITIC_SYSTEM, f"VERDICT TO CRITICIZE:\n{v.model_dump_json(indent=1)}"): {
            "component": "main", "overall": "flag", "confidence": 0.7,
            "motivation": "severita' ridotta dopo critica",
            "lines": [{"line": 0, "verdict": "flag", "reason": "dose",
                      "severity": "medium"}],
        },
    })
    revised = _run(critic_revise(fake, v))
    assert revised.overall == "flag"


def test_recomposition_dish() -> None:
    fake = FakeLLMClient(judgements={
        (("You are a corporate chef reviewing the whole dish. Check portion "
          "architecture (total mass, protein/starch/vegetable balance), "
          "class/diet coherence, line service feasibility, cross-cutting "
          "safety. Respond with a single JSON object matching the schema."),
         "COMPONENT VERDICTS:\n"): {
            "overall": "ok", "confidence": 0.85,
            "motivation": "equilibrio proteina/amido/verdura ok",
            "issues": [],
        },
    })
    v = _run(judge_recomposition(fake, []))
    assert v.overall == "ok"
