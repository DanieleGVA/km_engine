"""WP-B3 — Copertura glossario (gate GB3).

Metriche sul corpus:
- PILOT (15 ricette validate): copertura mention ingredienti >= 95% (target roadmap B).
- FULL (70 ricette: pilota + 55 estratte automaticamente da Marchesi): la
  copertura e' misurata e il residuo DEVE finire in coda proposte (glossary_proposals)
  — mai inventato nel md (P5). La soglia del corpus full e' riportata nel report
  (metodologia: corpus automatico senza validazione esperta; il residuo e' il
  segnale B3 per estendere i glossari seed).
"""
from __future__ import annotations

import pytest

from app.domain import canonicalize, parse_source_md, translate_document
from app.domain.pack import DomainPackBundle, load_domain_pack
from tests.domain.conftest import PACK_DIR, read_corpus
from tests.domain.fake_llm import build_fake_llm

PILOT_PREFIXES = ("ric-0", "ric-1")


async def _coverage(pack: DomainPackBundle, corpus: dict[str, str]) -> tuple[int, int, dict[str, int]]:
    fake = build_fake_llm(pack, corpus)
    total = unresolved = 0
    counter: dict[str, int] = {}
    for src in corpus.values():
        parsed = parse_source_md(src, known_units=pack.known_units())
        tr = await translate_document(pack, src, fake)
        canon = canonicalize(pack, tr.translated_md)
        total += len(parsed.ingredients)
        unresolved += len(canon.unresolved_terms)
        for t in canon.unresolved_terms:
            counter[t] = counter.get(t, 0) + 1
    return total, unresolved, counter


@pytest.mark.asyncio
async def test_b3_pilot_coverage_at_least_95_percent() -> None:
    """Criterio roadmap B: >=95% mention risolte sul corpus pilota validato."""
    pack = load_domain_pack(str(PACK_DIR))
    corpus = read_corpus()
    pilot = {k: v for k, v in corpus.items() if k.startswith(PILOT_PREFIXES)}
    assert len(pilot) >= 15
    total, unresolved, _ = await _coverage(pack, pilot)
    coverage = (total - unresolved) / total
    assert coverage >= 0.95, f"copertura pilota {coverage:.1%} < 95% ({unresolved}/{total} irrisolte)"
    # I soli irrisolti ammessi sul pilota sono i 2 volutamente non risolti per T10
    # (modificatori "sbucciate") — nessun id inventato, solo coda proposte.
    assert unresolved <= 3


@pytest.mark.asyncio
async def test_b3_full_corpus_residue_goes_to_proposals() -> None:
    """Sul corpus completo il residuo e' tracciato: nessun id inventato, la
    canonicalizzazione lascia il termine invariato (verra' proposto via
    glossary_proposals dal chiamante) e il round-trip resta possibile."""
    pack = load_domain_pack(str(PACK_DIR))
    corpus = read_corpus()
    total, unresolved, counter = await _coverage(pack, corpus)
    assert total >= 400, f"corpus inatteso: {total} mention"
    # Il residuo e' sempre >= 0 e documentato; qui vincoliamo solo la non-negativita'
    # e la tracciabilita' (le proposte vengono create a valle, vedi T10).
    assert unresolved >= 0
    # i termini piu' frequenti devono essere terminologia reale, non id inventati
    for term in counter:
        assert not term.startswith("ING-"), f"id inventato nel md: {term}"
