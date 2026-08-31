"""Passo 14 PROGRAMMA-UNICO: routing k=3 + batch-approve.

Obiettivo: la confidenza e' un fatto misurato (accordo tra esecuzioni), mai
un'autodichiarazione; il disaccordo va sempre all'umano.
"""
from __future__ import annotations

import asyncio

from app.domain.canon_judge import CANON_JUDGE_SYSTEM, build_component_prompt
from app.domain.llm import FakeLLMClient
from app.domain.routing import _permutations, route_k3

CANDIDATES = [
    {"document_id": "bk_a-0001", "title": "A", "lines": ["- 100 g x"]},
    {"document_id": "bk_b-0001", "title": "B", "lines": ["- 100 g x"]},
    {"document_id": "bk_c-0001", "title": "C", "lines": ["- 100 g x"]},
]
CARD = ["- 100 g x"]


def _run(coro):
    return asyncio.run(coro)


def _fake_for(verdicts: list[dict]) -> FakeLLMClient:
    """Fake che risponde in base all'ordine dei candidati nel prompt."""
    judgements = {}
    by_id = {c["document_id"]: c for c in CANDIDATES}
    perms = _permutations([c["document_id"] for c in CANDIDATES])
    for idx, perm in enumerate(perms):
        ordered = [by_id[cid] for cid in perm if cid in by_id]
        judgements[(CANON_JUDGE_SYSTEM, build_component_prompt("main", CARD, ordered))] = verdicts[idx]
    return FakeLLMClient(judgements=judgements)


def _verdict(overall="ok", line="ok") -> dict:
    return {
        "component": "main", "overall": overall, "confidence": 0.9,
        "motivation": "m", "lines": [{"line": 0, "verdict": line, "reason": "r"}],
    }


def test_permutations_deterministic_and_distinct() -> None:
    perms = _permutations(["a", "b", "c"])
    assert len(perms) == 3
    assert all(len(set(p)) == 3 for p in perms)
    # deterministiche
    assert _permutations(["a", "b", "c"]) == perms


def test_agreement_batch_approve() -> None:
    """Convergenza sui meccanici => batch-approve."""
    fake = _fake_for([_verdict(), _verdict(), _verdict()])
    res = _run(route_k3(fake, "main", CARD, CANDIDATES))
    assert res.route == "batch_approve"
    assert res.agreement


def test_divergence_goes_to_human() -> None:
    """Caso costruito divergente => coda umana anche se ogni run si dichiara
    sicuro (confidenza alta ma disaccordo)."""
    fake = _fake_for([_verdict("ok"), _verdict("flag", "flag"), _verdict("ok")])
    res = _run(route_k3(fake, "main", CARD, CANDIDATES))
    assert res.route == "human"
    assert not res.agreement
    # la confidenza auto-dichiarata (0.9) non autorizza da sola
    assert all(r.confidence > 0.8 for r in res.runs)
    assert res.route == "human"


def test_canon_gap_dedicated_queue() -> None:
    fake = _fake_for([_verdict("canon_gap"), _verdict("canon_gap"), _verdict("canon_gap")])
    res = _run(route_k3(fake, "main", CARD, CANDIDATES))
    assert res.route == "canon_gap"


def test_llm_confidence_never_authorizes_alone() -> None:
    """Nessun percorso in cui llm_confidence da sola autorizza."""
    fake = _fake_for([_verdict("ok"), _verdict("flag", "flag"), _verdict("ok")])
    res = _run(route_k3(fake, "main", CARD, CANDIDATES))
    # anche con confidenza alta, il disaccordo => human
    assert res.route == "human"
