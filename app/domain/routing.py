"""Passo 14 PROGRAMMA-UNICO: routing per accordo k=3.

La confidenza e' un fatto misurato (accordo tra esecuzioni), mai
un'autodichiarazione: k=3 esecuzioni con ordine dei candidati permutato.
Convergenza => batch-approve dei meccanici; divergenza => coda umana;
CANON_GAP => coda dedicata. Nessun percorso in cui llm_confidence da sola
autorizza un'applicazione.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.domain.canon_judge import ComponentVerdict, judge_component
from app.domain.llm import LLMClient

K = 3


@dataclass
class K3Result:
    """Esito del routing k=3."""

    runs: list[ComponentVerdict] = field(default_factory=list)
    permutations: list[list[str]] = field(default_factory=list)
    route: str = "human"  # batch_approve | human | canon_gap
    agreement: bool = False

    @property
    def llm_confidence_never_authorizes(self) -> bool:
        """La confidenza auto-dichiarata non autorizza mai da sola."""
        return self.route != "batch_approve" or self.agreement


def _permutations(candidate_ids: list[str]) -> list[list[str]]:
    """K permutazioni deterministiche dell'ordine dei candidati."""
    ids = list(candidate_ids)
    perms: list[list[str]] = []
    for seed in range(K):
        # hash-based shuffle deterministico (verificabile dai log)
        ordered = sorted(
            ids, key=lambda cid: hashlib.sha256(f"{seed}:{cid}".encode()).hexdigest()
        )
        perms.append(ordered)
    return perms


def _agreement(runs: list[ComponentVerdict]) -> bool:
    """Accordo: stesso overall e stessi verdetti per riga."""
    if not runs:
        return False
    first = runs[0]
    return all(
        r.overall == first.overall
        and [l.verdict for l in r.lines] == [l.verdict for l in first.lines]
        for r in runs
    )


async def route_k3(
    judge: LLMClient,
    component: str,
    card_lines: list[str],
    candidates: list[dict[str, Any]],
    house_rules: str = "",
) -> K3Result:
    """Esegue il giudice k=3 con ordine dei candidati permutato e instrada."""
    candidate_ids = [c["document_id"] for c in candidates]
    perms = _permutations(candidate_ids)
    runs: list[ComponentVerdict] = []
    by_id = {c["document_id"]: c for c in candidates}
    for perm in perms:
        # ordine dei candidati = ordine della permutazione (verificabile)
        ordered = [by_id[cid] for cid in perm if cid in by_id]
        runs.append(await judge_component(
            judge, component, card_lines, ordered, house_rules
        ))
    agree = _agreement(runs)
    if runs and runs[0].overall == "canon_gap":
        route = "canon_gap"
    elif agree:
        route = "batch_approve"
    else:
        route = "human"
    return K3Result(runs=runs, permutations=perms, route=route, agreement=agree)
