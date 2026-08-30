"""Shared deterministic FakeLLMClient builder for Iteration A tests.

The stage-1 translator is deterministic and glossary-based: it lowercases the
masked body and replaces glossary terms with their canonical English label.
``{Nk}`` placeholders are protected with a sentinel so ``normalize_terms``
(which lowercases its input) cannot turn them into ``{nk}`` and break the P2
re-injection.
"""
from __future__ import annotations

import re

from app.domain import (
    FakeLLMClient,
    build_translation_input,
    mask_numbers,
    normalize_terms,
    parse_source_md,
)
from app.domain.pack import DomainPackBundle

_PLACEHOLDER_RE = re.compile(r"\{N\d+\}")


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
    pack: DomainPackBundle, corpus: dict[str, str]
) -> FakeLLMClient:
    """Build a FakeLLMClient with one fixture per corpus document."""
    translations: dict[str, str] = {}
    for source_md in corpus.values():
        parsed = parse_source_md(source_md, known_units=pack.known_units())
        masked_input, _ = mask_numbers(build_translation_input(parsed))
        translations[masked_input] = translate_masked(pack, masked_input)
    return FakeLLMClient(translations)
