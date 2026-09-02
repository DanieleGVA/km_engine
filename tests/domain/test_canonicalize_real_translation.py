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

import json
import warnings

import pytest

from app.domain.coverage import measure_coverage, measure_documents
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
    # La struttura va dichiarata: lasciato libero, il modello traduce
    # "## Procedimento" in "## Procedure" — inglese corretto, illeggibile per
    # il parser (visto sul modello reale prima di aggiungere queste righe).
    assert "'## Ingredients'" in prompt
    assert "'## Method'" in prompt
    # ogni etichetta e' nel prompt: un elenco tagliato toglierebbe termini a
    # caso, e il mancante sarebbe proprio quello che il traduttore parafrasa
    for label in labels:
        assert f"\n- {label}" in prompt


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

def _corpus_outside_the_golden(size: int = 3) -> dict[str, str]:
    """Ricette che il golden NON copre: li' il fake deve ripiegare."""
    covered = {path.stem for path in GOLDEN_DIR.glob("*.md")}
    picked: dict[str, str] = {}
    for path in sorted(CORPUS_DIR.glob("*.md"), reverse=True):
        if path.stem in covered:
            continue
        picked[path.name] = path.read_text(encoding="utf-8")
        if len(picked) == size:
            break
    return picked


def _corpus_inside_the_golden(size: int = 3) -> dict[str, str]:
    covered = sorted(path.stem for path in GOLDEN_DIR.glob("*.md"))[:size]
    return {
        f"{stem}.md": (CORPUS_DIR / f"{stem}.md").read_text(encoding="utf-8")
        for stem in covered
    }


def test_f6_fake_llm_warns_when_it_falls_back_to_the_glossary(pack) -> None:
    with pytest.warns(CircularFakeLLMWarning, match="misura se"):
        build_fake_llm(pack, _corpus_outside_the_golden(), warn_on_fallback=True)


def test_f6_fake_llm_is_silent_by_default(pack, recwarn) -> None:
    """I test che non sono gate non devono annegare negli avvisi."""
    build_fake_llm(pack, _corpus_outside_the_golden())
    assert not [w for w in recwarn if w.category is CircularFakeLLMWarning]


@golden_required
def test_f6_fake_llm_prefers_the_golden(pack) -> None:
    """Sulle ricette coperte dal golden il fake non traduce piu' da glossario."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", CircularFakeLLMWarning)
        build_fake_llm(pack, _corpus_inside_the_golden(), warn_on_fallback=True)


# ---------------------------------------------------------------------------
# Il gate vero, quando il golden c'e'
# ---------------------------------------------------------------------------

@golden_required
def test_f6_translated_coverage_tracks_source_coverage(pack) -> None:
    """La copertura sul tradotto reale non crolla rispetto alla sorgente.

    Confronto sulle STESSE ricette: misurare il tradotto (153 documenti)
    contro il corpus intero (1.462) confronterebbe due campioni diversi.
    """
    manifest = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    sources = {
        entry["source"]: (CORPUS_DIR / entry["source"]).read_text(encoding="utf-8")
        for entry in manifest["documents"]
    }
    source = measure_documents(pack, sources, stage="source")
    translated = measure_coverage(pack, GOLDEN_DIR, stage="translated")
    assert translated.parse_errors == []
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
