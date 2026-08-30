"""T5 — L2 section comparison: divergence localized + L3 escalation."""
from __future__ import annotations

from app.domain import (
    IngredientLine,
    ParsedDoc,
    normalize_terms,
    parse_source_md,
    verify_l2,
)

from .conftest import read_corpus, real_recipe_names


def glossary_translate(text: str, pack) -> str:
    """Deterministic glossary-based translation used only to build L2 fixtures."""
    return normalize_terms(text, pack.it_to_en_terms())


def build_translated_parsed(pack, source: ParsedDoc, *, steps=None) -> ParsedDoc:
    return ParsedDoc(
        frontmatter={**source.frontmatter, "lang": "en", "source_lang": "it"},
        title=glossary_translate(source.title, pack),
        ingredients=[
            IngredientLine(
                raw=ing.raw,
                qty=ing.qty,
                unit=ing.unit,
                item=glossary_translate(ing.item, pack),
            )
            for ing in source.ingredients
        ],
        steps=list(source.steps) if steps is None else steps,
        body="",
        source_md="",
    )


def test_l2_real_recipes_pass_with_glossary_translation(pack) -> None:
    corpus = read_corpus()
    for name in real_recipe_names():
        source = parse_source_md(corpus[name], known_units=pack.known_units())
        translated = build_translated_parsed(pack, source)
        report = verify_l2(source, translated, pack=pack)
        assert report.passed, (name, [(s.section, s.overlap) for s in report.sections])


def test_l2_rewritten_steps_are_localized_and_escalated(pack) -> None:
    corpus = read_corpus()
    source = parse_source_md(
        corpus["ric-101-asparagi-burro.md"], known_units=pack.known_units()
    )
    rewritten_steps = [
        "Preheat the oven to 200 degrees.",
        "Bake for 45 minutes.",
        "Let cool completely.",
        "Serve chilled.",
        "Garnish with mint.",
    ]
    translated = build_translated_parsed(pack, source, steps=rewritten_steps)
    report = verify_l2(source, translated, pack=pack)

    assert not report.passed
    steps = {section.section: section for section in report.sections}
    assert steps["steps"].divergent
    assert not steps["ingredients"].divergent
    assert not steps["title"].divergent

    escalations = [issue for issue in report.escalations if issue.code == "ESCALATE_L3"]
    assert escalations
    assert all(issue.section == "steps" for issue in escalations)
