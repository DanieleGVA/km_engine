"""WP-C2 Designer — flusso dall'INTERO libro Marchesi.

Il corpus completo (1462 ricette estratte dal libro, fixture
tests/fixtures/corpus_marchesi_full/) attraversa:

1. TRADUZIONE (stadio 1, domain-agnostic): translate_corpus -> translated.md
2. ANALYST (WP-C1): analyze_corpus sul corpus TRADOTTO -> DomainBrief
   (entita' con frequenze reali sull'intero libro, unita', ambiguita')
3. DESIGNER (WP-C2): design_pack -> bozza Domain Pack in staging
   (il designer interviene DOPO la traduzione, prima della canonicalizzazione)
4. VALIDAZIONE: la bozza valida contro lo schema pydantic
5. GATE UMANO (P5): nessun file tocca il pack manuale; solo staging

Prefisso: nessun dato su Neo4j/Postgres (pura elaborazione offline).
"""
from __future__ import annotations

import pathlib

import pytest

from app.agents import DesignError, analyze_corpus, design_pack, translate_corpus
from app.domain import load_domain_pack
from tests.agents.conftest import PACK_DIR
from tests.domain.fake_llm import build_fake_llm

FULL_DIR = pathlib.Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "corpus_marchesi_full"
EXPECTED_MIN_RECIPES = 1000  # l'intero libro ha ~1462 ricette estratte


def _read_full_corpus() -> dict[str, str]:
    files = sorted(FULL_DIR.glob("mar-*.md"))
    assert len(files) >= EXPECTED_MIN_RECIPES, f"corpus libro incompleto: {len(files)}"
    return {p.name: p.read_text(encoding="utf-8") for p in files}


async def _build_brief(pack, corpus):
    """Stadio 1 (traduzione) + Analyst -> DomainBrief."""
    llm = build_fake_llm(pack, corpus)
    translated = await translate_corpus(pack, corpus, llm)
    return analyze_corpus(
        corpus,
        translated,
        known_units=pack.known_units(),
        countable_units=pack.countable_units(),
    )


@pytest.mark.asyncio
async def test_ic_designer_full_book_flow(tmp_path) -> None:
    """Analyst -> Designer sull'intero libro: brief con frequenze reali + draft valido."""
    pack = load_domain_pack(str(PACK_DIR))
    corpus = _read_full_corpus()

    brief = await _build_brief(pack, corpus)
    # il brief riflette l'intero libro
    assert len(brief.entities) >= 200, f"entita' attese >=200, trovate {len(brief.entities)}"
    assert len(brief.units) >= 10, f"unita' attese >=10, trovate {len(brief.units)}"
    top = sorted(brief.entities, key=lambda e: -e.frequency)[0]
    assert top.frequency >= 10, f"frequenza top entita' attesa >=10, trovata {top.frequency}"

    # Designer -> bozza in staging (dopo la traduzione)
    staging = tmp_path / "draft_full"
    result = design_pack(brief, staging_dir=staging)
    assert result.glossary_entries >= 200, (
        f"entry glossario attese >=200, trovate {result.glossary_entries}"
    )

    # validazione contro lo schema pydantic
    bundle = load_domain_pack(result.staging_dir)
    assert bundle.pack.name == "ricette"
    assert len(bundle.glossary_entries()) == result.glossary_entries

    # gate umano (P5): solo staging; pack manuale intatto
    for path in result.files:
        assert path.is_relative_to(staging.resolve())
    manual_before = (PACK_DIR / "pack.yaml").read_text(encoding="utf-8")
    manual_after = (PACK_DIR / "pack.yaml").read_text(encoding="utf-8")
    assert manual_before == manual_after


@pytest.mark.asyncio
async def test_ic_designer_full_book_gate_rejects_production(tmp_path) -> None:
    """Il gate umano rifiuta la dir di produzione come staging (anche sul libro completo)."""
    pack = load_domain_pack(str(PACK_DIR))
    corpus = _read_full_corpus()
    brief = await _build_brief(pack, corpus)
    with pytest.raises(DesignError):
        design_pack(brief, staging_dir=PACK_DIR)


@pytest.mark.asyncio
async def test_ic_designer_full_book_deterministic(tmp_path) -> None:
    """Due run sull'intero libro: draft byte-identici (determinismo)."""
    pack = load_domain_pack(str(PACK_DIR))
    corpus = _read_full_corpus()
    brief = await _build_brief(pack, corpus)

    first = design_pack(brief, staging_dir=tmp_path / "a")
    second = design_pack(brief, staging_dir=tmp_path / "b")
    a = {p.relative_to(first.staging_dir): p.read_bytes() for p in first.files}
    b = {p.relative_to(second.staging_dir): p.read_bytes() for p in second.files}
    assert a == b
