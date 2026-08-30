"""T3 — P2 number invariants: extraction, masking, re-injection, translation."""
from __future__ import annotations

import pytest

from app.domain import (
    FakeLLMClient,
    NumberInvariantError,
    build_translation_input,
    extract_numbers,
    mask_numbers,
    parse_source_md,
    reinject_numbers,
    translate_document,
)

from .conftest import read_corpus

SOURCE = """\
---
title: Pasta al pomodoro
id: IA-T3-001
lang: it
servings: 2
time_min: 20
difficulty: facile
---
## Ingredienti
- 200 g pomodori pelati
- 1 spicchio aglio
- 2 cucchiai olio extravergine di oliva

## Procedimento
1. Soffriggere l'aglio per 2 minuti.
2. Cuocere per 15 minuti a 180 gradi.
"""

TRANSLATED_MASKED = """\
Pasta with tomato
## Ingredients
- {N1} g peeled tomatoes
- {N2} clove garlic
- {N3} tablespoons extra virgin olive oil

## Method
1. Sauté the garlic for {N4} minutes.
2. Cook for {N5} minutes at {N6} degrees.
"""


def test_extract_numbers_real_recipes(pack) -> None:
    corpus = read_corpus()
    # Decimal quantity, °C with a space, hours, and no step numbers.
    assert extract_numbers(corpus["ric-101-asparagi-burro.md"]) == [
        "1.5", "50", "40", "1", "5", "4",
    ]
    assert extract_numbers(corpus["ric-103-amaretti.md"]) == [
        "120", "80", "160", "80", "4", "200", "4", "170", "15",
    ]
    # "farina 00" must not leak the flour type "00" as a number.
    assert "00" not in extract_numbers(corpus["ric-003-torta.md"])


def test_mask_and_reinject_roundtrip() -> None:
    text = "1. Cuocere per 15 minuti a 180 gradi.\n2. Servire.\n- 1.5 kg farina"
    masked, numbers = mask_numbers(text)
    assert numbers == ["15", "180", "1.5"]
    assert "15" not in masked
    assert "{N1}" in masked and "{N2}" in masked and "{N3}" in masked
    assert reinject_numbers(masked, numbers) == text


def test_reinject_rejects_missing_placeholder() -> None:
    with pytest.raises(ValueError, match="placeholder sequence"):
        reinject_numbers("Cook for 15 minutes.", ["15"])


async def test_translate_document_preserves_numbers(pack) -> None:
    parsed = parse_source_md(SOURCE, known_units=pack.known_units())
    masked_input, numbers = mask_numbers(build_translation_input(parsed))
    assert numbers == ["200", "1", "2", "2", "15", "180"]

    llm = FakeLLMClient({masked_input: TRANSLATED_MASKED})
    result = await translate_document(pack, SOURCE, llm)

    assert result.document_id == "IA-T3-001"
    assert result.title_en == "Pasta with tomato"
    assert "lang: en" in result.translated_md
    assert "source_lang: it" in result.translated_md
    assert "difficulty: easy" in result.translated_md
    assert "## Ingredients" in result.translated_md
    assert "## Method" in result.translated_md
    assert "- 200 g peeled tomatoes" in result.translated_md
    assert "2. Cook for 15 minutes at 180 degrees." in result.translated_md


async def test_translate_document_dropped_number_fails(pack) -> None:
    parsed = parse_source_md(SOURCE, known_units=pack.known_units())
    masked_input, _ = mask_numbers(build_translation_input(parsed))
    corrupted = TRANSLATED_MASKED.replace("{N5}", "")
    llm = FakeLLMClient({masked_input: corrupted})
    with pytest.raises(NumberInvariantError):
        await translate_document(pack, SOURCE, llm)


async def test_translate_document_extra_number_fails(pack) -> None:
    parsed = parse_source_md(SOURCE, known_units=pack.known_units())
    masked_input, _ = mask_numbers(build_translation_input(parsed))
    corrupted = TRANSLATED_MASKED.replace(
        "{N5} minutes", "{N5} minutes and 16 seconds"
    )
    llm = FakeLLMClient({masked_input: corrupted})
    with pytest.raises(NumberInvariantError):
        await translate_document(pack, SOURCE, llm)
