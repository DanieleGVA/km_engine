"""Stage 1 translation (WP-A2): P2-safe Italian -> English.

``translate_document`` extracts content numbers, masks them as ``{Nk}``
placeholders, asks the LLM to translate the masked body, re-injects the numbers
and verifies the P2 multiset invariant before returning a
:class:`TranslatedDocument`.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.domain.errors import NumberInvariantError
from app.domain.llm import LLMClient
from app.domain.numbers import (
    extract_numbers,
    mask_numbers,
    numbers_multiset_equal,
    reinject_numbers,
)
from app.domain.pack import DomainPackBundle
from app.domain.verify import (
    DIFFICULTY_MAP,
    ParsedDoc,
    parse_source_md,
    parse_translated_body,
    render_ingredient_line,
)


@dataclass
class TranslatedDocument:
    """Result of stage-1 translation."""

    source_md: str
    translated_md: str
    source_lang: str
    target_lang: str
    numbers: list[str]
    document_id: str
    title_en: str


def build_translation_input(parsed: ParsedDoc) -> str:
    """Rebuild the body sent to the LLM (title + sections, no frontmatter)."""
    lines = [parsed.title, "", "## Ingredienti"]
    lines.extend(f"- {ing.raw}" for ing in parsed.ingredients)
    lines.append("")
    lines.append("## Procedimento")
    lines.extend(f"{index}. {step}" for index, step in enumerate(parsed.steps, start=1))
    return "\n".join(lines)


def render_translated_document(
    source: ParsedDoc,
    translated: ParsedDoc,
    pack: DomainPackBundle,
) -> str:
    """Render the translated markdown in the Appendix A stage-1 shape."""
    frontmatter = {
        "title": translated.title,
        "id": source.frontmatter["id"],
        "lang": pack.canonical_language,
        "source_lang": pack.language,
        "servings": source.frontmatter["servings"],
        "time_min": source.frontmatter["time_min"],
        "difficulty": DIFFICULTY_MAP[source.frontmatter["difficulty"]],
    }
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in frontmatter.items())
    lines.append("---")
    lines.append("## Ingredients")
    lines.extend(
        render_ingredient_line(ingredient) for ingredient in translated.ingredients
    )
    lines.append("## Method")
    lines.extend(
        f"{index}. {step}" for index, step in enumerate(translated.steps, start=1)
    )
    return "\n".join(lines) + "\n"


async def translate_document(
    pack: DomainPackBundle,
    source_md: str,
    llm: LLMClient,
) -> TranslatedDocument:
    """Translate an Italian source document to English, P2-safe.

    Raises :class:`NumberInvariantError` when the translated document does not
    contain exactly the same multiset of content numbers as the source.
    """
    units = pack.known_units()
    countable = pack.countable_units()
    source = parse_source_md(
        source_md, known_units=units, countable_units=countable
    )
    source_numbers = extract_numbers(source_md)

    translation_input = build_translation_input(source)
    masked_input, numbers = mask_numbers(translation_input)

    translated_masked = await llm.translate(
        masked_input,
        source_lang=pack.language,
        target_lang=pack.canonical_language,
    )

    try:
        restored_body = reinject_numbers(translated_masked, numbers)
    except ValueError as exc:
        raise NumberInvariantError(
            f"P2 placeholder re-injection failed: {exc}"
        ) from exc

    title_en, ingredients_en, steps_en = parse_translated_body(
        restored_body, known_units=units, countable_units=countable
    )
    translated = ParsedDoc(
        frontmatter={
            "title": title_en,
            "id": source.frontmatter["id"],
            "lang": pack.canonical_language,
            "source_lang": pack.language,
            "servings": source.frontmatter["servings"],
            "time_min": source.frontmatter["time_min"],
            "difficulty": DIFFICULTY_MAP[source.frontmatter["difficulty"]],
        },
        title=title_en,
        ingredients=ingredients_en,
        steps=steps_en,
        body=restored_body,
        source_md="",
    )

    translated_md = render_translated_document(source, translated, pack)
    translated_numbers = extract_numbers(translated_md)

    if not numbers_multiset_equal(source_numbers, translated_numbers):
        raise NumberInvariantError(
            f"P2 number multiset mismatch: source={source_numbers} "
            f"translated={translated_numbers}"
        )

    return TranslatedDocument(
        source_md=source_md,
        translated_md=translated_md,
        source_lang=pack.language,
        target_lang=pack.canonical_language,
        numbers=source_numbers,
        document_id=source.frontmatter["id"],
        title_en=translated.title,
    )
