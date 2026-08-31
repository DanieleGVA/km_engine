"""Passo 16 PROGRAMMA-UNICO: end-to-end su batch reale con gate chef.

Obiettivo: la catena e' spiegata e reversibile; diff coperto dai log;
rollback da log ricostruisce l'originale; nessuna modifica da verdetto non
approvato.
"""
from __future__ import annotations

import asyncio

from app.domain import load_domain_pack
from app.domain.canon_judge import CANON_JUDGE_SYSTEM, build_component_prompt
from app.domain.e2e import (
    _card_components,
    rollback_from_log,
    run_e2e_batch,
    verify_log_coverage,
)
from app.domain.llm import FakeLLMClient
from app.domain.routing import _permutations
from tests.domain.conftest import PACK_DIR

CARD = {
    "id": "RF0001",
    "canonical_md": "## Ingredients\n- 100 g chicken\n- 50 g rice\n## Method\n1. Cook.",
    "candidates": [{"document_id": "bk_x-0001", "title": "X", "lines": ["- 100 g chicken"]}],
}


def _run(coro):
    return asyncio.run(coro)


def _fake_ok() -> FakeLLMClient:
    verdict = {
        "component": "main", "overall": "ok", "confidence": 0.9,
        "motivation": "m", "lines": [{"line": 0, "verdict": "ok", "reason": "r",
                                     "citation": "bk_x-0001:0", "base": "ratio"}],
    }
    judgements = {}
    by_id = {c["document_id"]: c for c in CARD["candidates"]}
    for perm in _permutations([c["document_id"] for c in CARD["candidates"]]):
        ordered = [by_id[cid] for cid in perm if cid in by_id]
        judgements[(CANON_JUDGE_SYSTEM, build_component_prompt("main", ["- 100 g chicken", "- 50 g rice"], ordered))] = verdict
    return FakeLLMClient(judgements=judgements)


def test_card_components() -> None:
    comps = _card_components(CARD["canonical_md"])
    assert comps == [("main", ["- 100 g chicken", "- 50 g rice"])]


def test_log_coverage() -> None:
    log = [
        {"before_text": "- 100 g chicken", "after_text": "- 120 g chicken"},
    ]
    problems = verify_log_coverage(
        "- 100 g chicken", "- 120 g chicken", log)
    assert problems == []
    problems2 = verify_log_coverage(
        "- 100 g chicken", "- 120 g chicken", [])
    assert problems2  # differenza orfana


def test_rollback_reconstructs() -> None:
    log = [
        {"before_text": "- 100 g chicken", "after_text": "- 120 g chicken"},
        {"before_text": "- 50 g rice", "after_text": "- 60 g rice"},
    ]
    output = "- 120 g chicken\n- 60 g rice"
    original = rollback_from_log(log, output)
    assert original == "- 100 g chicken\n- 50 g rice"


def test_e2e_batch_writes_log(pg_conn) -> None:
    """La run end-to-end scrive il log solo per i verdetti approvati."""
    pack = load_domain_pack(str(PACK_DIR))
    fake = _fake_ok()
    result = _run(run_e2e_batch(fake, [CARD], pack, pg_conn))
    assert result.processed == 1
    assert result.batch_approved == 1
    assert result.log_entries == 1
    assert result.human == 0
    # pulizia
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM canon_adjudication_log WHERE document_id = 'RF0001'")
