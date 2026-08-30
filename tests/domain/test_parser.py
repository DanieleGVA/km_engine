"""T1 — template parser: valid corpus + explicit malformed cases."""
from __future__ import annotations

import pytest

from app.domain import ParseError, parse_source_md

from .conftest import read_corpus


@pytest.mark.parametrize("name", sorted(read_corpus().keys()))
def test_corpus_recipes_parse(pack, name) -> None:
    md = read_corpus()[name]
    parsed = parse_source_md(md, known_units=pack.known_units())
    assert parsed.title
    assert parsed.ingredients
    assert parsed.steps
    assert parsed.frontmatter["id"]


def test_real_recipe_decimal_and_celsius_parse(pack) -> None:
    md = read_corpus()["ric-101-asparagi-burro.md"]
    parsed = parse_source_md(md, known_units=pack.known_units())
    assert parsed.ingredients[0].qty == "1.5"
    assert parsed.ingredients[0].unit == "kg"
    assert parsed.ingredients[0].item == "asparagi"


def test_real_recipe_qty_without_unit(pack) -> None:
    md = read_corpus()["ric-103-amaretti.md"]
    parsed = parse_source_md(md, known_units=pack.known_units())
    albumi = parsed.ingredients[-1]
    assert albumi.qty == "4"
    assert albumi.unit is None
    assert albumi.item == "albumi"


def test_missing_frontmatter_raises() -> None:
    with pytest.raises(ParseError, match="frontmatter"):
        parse_source_md("## Ingredienti\n- 1 g sale\n## Procedimento\n1. x\n")


def test_missing_required_key_raises() -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g sale\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="time_min"):
        parse_source_md(md)


def test_missing_ingredients_section_raises() -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="Ingredienti"):
        parse_source_md(md)


def test_sections_out_of_order_raise() -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Procedimento\n1. x\n"
        "## Ingredienti\n- 1 g sale\n"
    )
    with pytest.raises(ParseError, match="must come after"):
        parse_source_md(md)


def test_ingredient_without_quantity_raises() -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- sale\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="quantity"):
        parse_source_md(md)


def test_step_numbering_gap_raises() -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g sale\n"
        "## Procedimento\n1. x\n3. y\n"
    )
    with pytest.raises(ParseError, match="expected step 2"):
        parse_source_md(md)


def test_empty_ingredient_item_raises() -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="item is empty"):
        parse_source_md(md)
