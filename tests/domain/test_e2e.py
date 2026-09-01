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


def _fake_mixed() -> FakeLLMClient:
    """Componente A approvato, componente B canon_gap (candidati propri)."""
    cand_a = [{"document_id": "bk_a-0001", "title": "A", "lines": ["- 100 g chicken"]}]
    cand_b = [{"document_id": "bk_b-0001", "title": "B", "lines": ["- 50 g rice"]}]
    ok = {
        "component": "protein", "overall": "ok", "confidence": 0.9,
        "motivation": "m", "lines": [{"line": 0, "verdict": "ok", "reason": "r",
                                     "citation": "bk_a-0001:0", "base": "ratio"}],
    }
    gap = {
        "component": "starch", "overall": "canon_gap", "confidence": 0.4,
        "motivation": "no candidate covers the component", "lines": [],
    }
    judgements = {}
    for perm in _permutations([c["document_id"] for c in cand_a]):
        ordered = [cand_a[0] if cid == "bk_a-0001" else None for cid in perm]
        ordered = [c for c in ordered if c]
        judgements[(CANON_JUDGE_SYSTEM, build_component_prompt(
            "protein", ["- 100 g chicken"], ordered))] = ok
    for perm in _permutations([c["document_id"] for c in cand_b]):
        ordered = [cand_b[0] if cid == "bk_b-0001" else None for cid in perm]
        ordered = [c for c in ordered if c]
        judgements[(CANON_JUDGE_SYSTEM, build_component_prompt(
            "starch", ["- 50 g rice"], ordered))] = gap
    return FakeLLMClient(judgements=judgements)


def test_e2e_batch_resume_skips_judged(pg_conn) -> None:
    """Resume: un componente gia' giudicato (coda umana o log) viene saltato,
    non duplicato."""
    pack = load_domain_pack(str(PACK_DIR))
    card = {
        "id": "RF0003",
        "canonical_md": "## Ingredients\n- 100 g chicken\n## Method\n1. Cook.",
        "components": [
            {"name": "protein", "lines": ["- 100 g chicken"],
             "candidates": [{"document_id": "bk_a-0001", "title": "A",
                              "lines": ["- 100 g chicken"]}]},
        ],
    }
    # prima run: giudica e scrive nel log (batch_approve)
    r1 = _run(run_e2e_batch(_fake_mixed(), [card], pack, pg_conn))
    assert r1.batch_approved == 1
    assert r1.log_entries == 1
    # seconda run (stessa card): il componente e' gia' nel log => skip
    r2 = _run(run_e2e_batch(_fake_mixed(), [card], pack, pg_conn))
    assert r2.skipped == 1
    assert r2.batch_approved == 0
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM canon_adjudication_log "
            "WHERE document_id = 'RF0003'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "DELETE FROM canon_adjudication_log WHERE document_id = 'RF0003'")


def test_e2e_batch_components_path(pg_conn) -> None:
    """Con componenti espliciti il giudice giudica PER componente, ognuno
    con i propri candidati: approvato -> log, canon_gap -> coda umana."""
    pack = load_domain_pack(str(PACK_DIR))
    card = {
        "id": "RF0002",
        "canonical_md": "## Ingredients\n- 100 g chicken\n- 50 g rice\n## Method\n1. Cook.",
        "components": [
            {"name": "protein", "lines": ["- 100 g chicken"],
             "candidates": [{"document_id": "bk_a-0001", "title": "A",
                              "lines": ["- 100 g chicken"]}]},
            {"name": "starch", "lines": ["- 50 g rice"],
             "candidates": [{"document_id": "bk_b-0001", "title": "B",
                              "lines": ["- 50 g rice"]}]},
        ],
    }
    result = _run(run_e2e_batch(_fake_mixed(), [card], pack, pg_conn))
    assert result.processed == 1
    assert result.components == 2
    assert result.batch_approved == 1
    assert result.canon_gap == 1
    assert result.human == 0
    assert result.log_entries == 1
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM canon_adjudication_log WHERE document_id = 'RF0002'")
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM adjudications WHERE document_id = 'RF0002' "
            "AND kind = 'canon' AND status = 'pending'")
        assert cur.fetchone()[0] == 1
        cur.execute("DELETE FROM canon_adjudication_log WHERE document_id = 'RF0002'")
        cur.execute("DELETE FROM adjudications WHERE document_id = 'RF0002'")
