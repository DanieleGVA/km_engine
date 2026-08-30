"""T4 — L1 verification: structure + P2 numbers on synthetic corruptions."""
from __future__ import annotations

from app.domain import DIFFICULTY_MAP, parse_source_md, verify_l1

from .conftest import read_corpus, real_recipe_names


def mechanical_translate(pack, source_md: str) -> str:
    """Build a structurally valid L1 translation (same content, EN sections)."""
    parsed = parse_source_md(source_md, known_units=pack.known_units())
    fm = parsed.frontmatter
    lines = [
        "---",
        f"title: {fm['title']}",
        f"id: {fm['id']}",
        "lang: en",
        f"source_lang: {fm['lang']}",
        f"servings: {fm['servings']}",
        f"time_min: {fm['time_min']}",
        f"difficulty: {DIFFICULTY_MAP[fm['difficulty']]}",
        "---",
        "## Ingredients",
    ]
    lines.extend(f"- {ing.raw}" for ing in parsed.ingredients)
    lines.append("## Method")
    lines.extend(f"{i}. {step}" for i, step in enumerate(parsed.steps, start=1))
    return "\n".join(lines) + "\n"


def test_l1_real_recipes_pass(pack) -> None:
    corpus = read_corpus()
    for name in real_recipe_names():
        report = verify_l1(corpus[name], mechanical_translate(pack, corpus[name]), pack=pack)
        assert report.passed, (name, [issue.message for issue in report.issues])


def test_l1_number_altered_is_caught(pack) -> None:
    corpus = read_corpus()
    source = corpus["ric-101-asparagi-burro.md"]
    translated = mechanical_translate(pack, source).replace(
        "- 50 g grana grattugiato", "- 51 g grana grattugiato"
    )
    report = verify_l1(source, translated, pack=pack)
    assert not report.passed
    assert any(issue.code == "P2_NUMBER_INVARIANT" for issue in report.issues)


def test_l1_ingredient_removed_is_caught(pack) -> None:
    corpus = read_corpus()
    source = corpus["ric-102-fregola-vongole.md"]
    translated = mechanical_translate(pack, source).replace(
        "- 1 spicchio aglio\n", ""
    )
    report = verify_l1(source, translated, pack=pack)
    assert not report.passed
    assert any(issue.code == "INGREDIENT_COUNT_MISMATCH" for issue in report.issues)


def test_l1_step_added_is_caught(pack) -> None:
    corpus = read_corpus()
    source = corpus["ric-103-amaretti.md"]
    translated = mechanical_translate(pack, source).replace(
        "7. Far raffreddare gli amaretti prima di servirli.\n",
        "7. Far raffreddare gli amaretti prima di servirli.\n8. Extra step.\n",
    )
    report = verify_l1(source, translated, pack=pack)
    assert not report.passed
    assert any(issue.code == "STEP_COUNT_MISMATCH" for issue in report.issues)


def test_l1_frontmatter_difficulty_mismatch_is_caught(pack) -> None:
    corpus = read_corpus()
    source = corpus["ric-101-asparagi-burro.md"]
    translated = mechanical_translate(pack, source).replace(
        "difficulty: easy", "difficulty: medium"
    )
    report = verify_l1(source, translated, pack=pack)
    assert not report.passed
    assert any(issue.code == "DIFFICULTY_MISMATCH" for issue in report.issues)
