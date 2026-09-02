"""WP-F6 — il gate non deve misurare se' stesso (D7).

Il traduttore finto sostituisce i termini usando lo stesso glossario che lo
stadio 2 usera' per risolverli: per costruzione non puo' mai mancare un
termine. Un gate misurato li' e' circolare.

Questi test verificano due cose:
  1. il fake dichiara quando sta ripiegando sulla sostituzione da glossario,
     cosi' un gate puo' pretendere il golden;
  2. quando il golden reale esiste, la copertura sul tradotto non e'
     sensibilmente peggiore di quella sulla sorgente — e se lo e', il report
     dice quali termini inglesi mancano (sono gli alias EN da aggiungere in
     F5, l'unico caso in cui il piano ammette alias per copertura).
"""
from __future__ import annotations

import pytest

from app.domain.coverage import measure_coverage
from app.domain.llm import (
    build_translation_system_prompt,
    translation_prompt_sha256,
)
from app.domain.translate import glossary_labels
from tests.domain.conftest import REPO_ROOT
from tests.domain.fake_llm import (
    GOLDEN_DIR,
    GOLDEN_MANIFEST,
    CircularFakeLLMWarning,
    build_fake_llm,
    load_golden_translations,
)

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"
MAX_COVERAGE_DROP = 0.03

golden_required = pytest.mark.skipif(
    not GOLDEN_MANIFEST.is_file(),
    reason=(
        "golden di traduzione reale assente: generalo con "
        "scripts/build_translated_golden.py (serve KM_LLM_*)"
    ),
)


# ---------------------------------------------------------------------------
# Il prompt vincolato
# ---------------------------------------------------------------------------

def test_f6_translation_prompt_carries_the_glossary(pack) -> None:
    """Le etichette canoniche entrano nel prompt: e' il fix di D7 lato LLM."""
    labels = glossary_labels(pack)
    assert labels
    prompt = build_translation_system_prompt("it", "en", labels)
    assert "{Nk}" in prompt
    assert "extra virgin olive oil" in prompt
    assert "do not substitute a synonym" in prompt
    # ogni etichetta e' nel prompt: un elenco tagliato toglierebbe termini a
    # caso, e il mancante sarebbe proprio quello che il traduttore parafrasa
    assert prompt.count("\n- ") == len(labels)


def test_f6_prompt_hash_changes_with_the_glossary(pack) -> None:
    """Il manifest del golden si accorge se il prompt e' cambiato."""
    labels = glossary_labels(pack)
    base = translation_prompt_sha256("it", "en", labels)
    assert base == translation_prompt_sha256("it", "en", labels)
    assert base != translation_prompt_sha256("it", "en", [*labels, "new term"])
    assert base != translation_prompt_sha256("it", "en", None)


# ---------------------------------------------------------------------------
# La circolarita' e' dichiarata
# ---------------------------------------------------------------------------

def _sample_corpus() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_DIR.glob("*.md"))[:3]
    }


def test_f6_fake_llm_warns_when_it_falls_back_to_the_glossary(pack) -> None:
    with pytest.warns(CircularFakeLLMWarning, match="misura se"):
        build_fake_llm(pack, _sample_corpus(), warn_on_fallback=True)


def test_f6_fake_llm_is_silent_by_default(pack, recwarn) -> None:
    """I test che non sono gate non devono annegare negli avvisi."""
    build_fake_llm(pack, _sample_corpus())
    assert not [w for w in recwarn if w.category is CircularFakeLLMWarning]


# ---------------------------------------------------------------------------
# Il gate vero, quando il golden c'e'
# ---------------------------------------------------------------------------

@golden_required
def test_f6_translated_coverage_tracks_source_coverage(pack) -> None:
    """La copertura sul tradotto reale non crolla rispetto alla sorgente."""
    source = measure_coverage(pack, CORPUS_DIR, stage="source")
    translated = measure_coverage(pack, GOLDEN_DIR, stage="translated")
    drop = source.coverage - translated.coverage
    assert drop <= MAX_COVERAGE_DROP, (
        f"copertura sorgente {source.coverage:.2%}, tradotto "
        f"{translated.coverage:.2%} (calo {drop:.2%}). Termini inglesi non "
        "mappati (da aggiungere come alias EN in F5): "
        f"{[term.term for term in translated.unresolved[:20]]}"
    )


@golden_required
def test_f6_golden_parses_and_is_used_by_the_fake(pack) -> None:
    """Il golden e' leggibile e il fake lo preferisce alla sostituzione."""
    translations = load_golden_translations()
    assert translations
    report = measure_coverage(pack, GOLDEN_DIR, stage="translated")
    assert report.parse_errors == []
    assert report.docs_parsed > 0
