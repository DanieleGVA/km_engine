"""Shared deterministic FakeLLMClient builder for Iteration A tests.

Il traduttore finto sostituisce i termini di glossario con la loro etichetta
canonica. E' circolare per costruzione: usa lo stesso glossario che lo stadio 2
usera' per risolvere, quindi non puo' mai mancare un termine (D7). Serve
comunque, perche' i test non devono chiamare la rete.

WP-F6: quando esiste il golden reale (``tests/fixtures/corpus_marchesi_translated``,
prodotto da ``scripts/build_translated_golden.py`` con l'LLM vero), il fake lo
legge e restituisce quella traduzione. Il fallback circolare resta, ma avvisa:
un gate misurato sul fallback misura se' stesso.

``{Nk}`` placeholders are protected with a sentinel so ``normalize_terms``
(which lowercases its input) cannot turn them into ``{nk}`` and break the P2
re-injection.
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

from app.domain import (
    FakeLLMClient,
    build_translation_input,
    mask_numbers,
    normalize_terms,
    parse_source_md,
)
from app.domain.pack import DomainPackBundle

_PLACEHOLDER_RE = re.compile(r"\{N\d+\}")

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLDEN_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_translated"
GOLDEN_MANIFEST = GOLDEN_DIR / "manifest.json"


class CircularFakeLLMWarning(UserWarning):
    """Il fake sta traducendo col glossario: il gate misurerebbe se' stesso."""


def load_golden_translations() -> dict[str, str]:
    """``{masked_input: masked_output}`` dal golden reale, se esiste.

    Il valore e' la risposta GREZZA del modello (corpo mascherato tradotto),
    non il documento finale: e' quello che un LLMClient restituisce, e solo
    cosi' la pipeline esegue davvero re-iniezione dei numeri e verifica P2.
    Restituire il documento gia' composto salterebbe quei passi.
    """
    if not GOLDEN_MANIFEST.is_file():
        return {}
    manifest = json.loads(GOLDEN_MANIFEST.read_text(encoding="utf-8"))
    return {
        entry["masked_input"]: entry["masked_output"]
        for entry in manifest.get("documents", [])
        if entry.get("masked_output")
    }


def translate_masked(pack: DomainPackBundle, masked_input: str) -> str:
    """Deterministic glossary-based translation of a masked body."""
    placeholders = _PLACEHOLDER_RE.findall(masked_input)
    protected = _PLACEHOLDER_RE.sub("\x00", masked_input)
    lines: list[str] = []
    for line in protected.splitlines():
        stripped = line.strip()
        if stripped == "## Ingredienti":
            lines.append("## Ingredients")
        elif stripped == "## Procedimento":
            lines.append("## Method")
        else:
            lines.append(normalize_terms(line, pack.it_to_en_terms()))
    translated = "\n".join(lines)
    for placeholder in placeholders:
        translated = translated.replace("\x00", placeholder, 1)
    return translated


def build_fake_llm(
    pack: DomainPackBundle,
    corpus: dict[str, str],
    *,
    prefer_golden: bool = True,
    warn_on_fallback: bool = False,
) -> FakeLLMClient:
    """Build a FakeLLMClient with one fixture per corpus document.

    Con ``prefer_golden`` usa la traduzione reale quando il golden la ha; per
    i documenti che il golden non copre ripiega sulla sostituzione da
    glossario. ``warn_on_fallback`` fa emettere
    :class:`CircularFakeLLMWarning` in quel caso, cosi' un test di gate puo'
    pretendere il golden con ``pytest.warns`` o ``filterwarnings("error")``.
    """
    golden = load_golden_translations() if prefer_golden else {}
    translations: dict[str, str] = {}
    fallbacks = 0
    for source_md in corpus.values():
        parsed = parse_source_md(
            source_md,
            known_units=pack.known_units(),
            countable_units=pack.countable_units(),
        )
        masked_input, _ = mask_numbers(build_translation_input(parsed))
        if masked_input in golden:
            translations[masked_input] = golden[masked_input]
            continue
        fallbacks += 1
        translations[masked_input] = translate_masked(pack, masked_input)
    if fallbacks and warn_on_fallback:
        warnings.warn(
            f"{fallbacks}/{len(corpus)} documenti tradotti dal glossario e non "
            "dal golden reale: un gate misurato qui misura se' stesso (D7). "
            "Genera il golden con scripts/build_translated_golden.py.",
            CircularFakeLLMWarning,
            stacklevel=2,
        )
    return FakeLLMClient(translations)
