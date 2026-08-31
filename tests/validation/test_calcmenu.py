"""Test normalizzatore ingredienti CalcMenu (fix I3-b).

Regola: mai forzare una mappatura quando il vocabolario non contiene il
termine giusto. Un nome non risolto resta identity e finisce in
``proposals`` (coda proposte glossario), mai mappato a un termine errato
("trout white fillet" -> "sole fillets" e' vietato).
"""
from __future__ import annotations

from app.validation.calcmenu import (
    CalcMenuNormalizer,
    load_canonical_vocab,
    match_glossary,
    normalize_deterministic,
)


class FakeLLM:
    """LLM deterministico per i test: risponde sempre ``answer``."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def translate(self, text: str, *, source_lang: str, target_lang: str) -> str:
        return self.answer


def test_deterministic_match() -> None:
    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=None)
    term, method = nz.normalize("olive oil")
    assert term == "olive oil"
    assert method == "deterministic"
    assert nz.proposals == set()


def test_unresolved_returns_identity_and_proposal() -> None:
    """Senza LLM, un nome industriale non risolto resta identity e va in coda."""
    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=None)
    term, method = nz.normalize("peas green frz")
    assert method == "identity"
    assert term == "peas green frz"
    assert "peas green frz" in nz.proposals


def test_llm_no_match_is_not_forced() -> None:
    """LLM che dichiara NO_MATCH: identity + proposta, mai termine forzato."""
    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=FakeLLM("NO_MATCH"))
    term, method = nz.normalize("peas green frz")
    assert method == "identity"
    assert "peas green frz" in nz.proposals


def test_llm_out_of_vocab_answer_is_not_forced() -> None:
    """LLM che risponde con un termine FUORI vocabolario: non viene usato."""
    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=FakeLLM("trout"))
    term, method = nz.normalize("trout white fillet")
    assert method == "identity"
    assert "trout white fillet" in nz.proposals


def test_llm_valid_answer_is_used_and_cached() -> None:
    """LLM che risponde con un termine NEL vocabolario: usato e messo in cache."""
    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=FakeLLM("hard-boiled eggs"))
    term, method = nz.normalize("hard-boiled eggs")
    assert term == "hard-boiled eggs"
    assert method == "llm"
    # seconda chiamata: cache, nessuna nuova richiesta
    term2, method2 = nz.normalize("hard-boiled eggs")
    assert term2 == "hard-boiled eggs"
    assert method2 == "llm-cache"
    assert nz.proposals == set()


def test_llm_unresolved_is_cached_as_none() -> None:
    """Un NO_MATCH viene cachato come None: non si ri-chiede all'LLM."""
    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=FakeLLM("NO_MATCH"))
    nz.normalize("peas green frz")
    assert nz.cache["peas green frz"] is None
    # seconda chiamata: cache None -> identity, senza ri-chiedere
    term, method = nz.normalize("peas green frz")
    assert method == "identity"
    assert "peas green frz" in nz.proposals


def test_llm_works_from_async_context() -> None:
    """Fix: l'LLM deve rispondere anche quando il normalizzatore e' chiamato
    da un workflow async (prima: asyncio.run falliva -> LLM mai usato)."""
    import asyncio

    nz = CalcMenuNormalizer(load_canonical_vocab(), llm=FakeLLM("hard-boiled eggs"))

    async def run() -> tuple[str, str]:
        return nz.normalize("hard-boiled eggs")

    term, method = asyncio.run(run())
    assert term == "hard-boiled eggs"
    assert method == "llm"
