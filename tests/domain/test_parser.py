"""T1 — template parser: valid corpus + explicit malformed cases."""
from __future__ import annotations

import pytest

from app.domain import ParseError, parse_source_md
from app.domain.quantities import TO_TASTE_IT
from app.domain.verify import _parse_ingredient, render_ingredient_line

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


def test_missing_frontmatter_raises(pack) -> None:
    with pytest.raises(ParseError, match="frontmatter"):
        parse_source_md(
            "## Ingredienti\n- 1 g sale\n## Procedimento\n1. x\n",
            known_units=pack.known_units(),
        )


def test_missing_required_key_raises(pack) -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g sale\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="time_min"):
        parse_source_md(md, known_units=pack.known_units())


def test_missing_ingredients_section_raises(pack) -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="Ingredienti"):
        parse_source_md(md, known_units=pack.known_units())


def test_sections_out_of_order_raise(pack) -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Procedimento\n1. x\n"
        "## Ingredienti\n- 1 g sale\n"
    )
    with pytest.raises(ParseError, match="must come after"):
        parse_source_md(md, known_units=pack.known_units())


def test_f3_ingredient_without_quantity_is_to_taste(pack) -> None:
    """WP-F3: una riga senza dose e' valida e vale "q.b.", non un errore.

    Prima il parser pretendeva ``^\\d+`` su ogni riga; per soddisfarlo il
    corpus era stato riscritto iniettando "1 pizzico" su 2.160 righe, cioe'
    inventando una dose che il libro non dava (D4).
    """
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- sale\n"
        "## Procedimento\n1. x\n"
    )
    parsed = parse_source_md(md, known_units=pack.known_units())
    ingredient = parsed.ingredients[0]
    assert ingredient.qty is None
    assert ingredient.to_taste is True
    assert ingredient.item == "sale"


def test_f3_empty_ingredient_still_raises(pack) -> None:
    """Una riga davvero vuota resta un errore: assenza di dose != assenza di riga."""
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- q.b.\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="empty"):
        parse_source_md(md, known_units=pack.known_units())


def test_step_numbering_gap_raises(pack) -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g sale\n"
        "## Procedimento\n1. x\n3. y\n"
    )
    with pytest.raises(ParseError, match="expected step 2"):
        parse_source_md(md, known_units=pack.known_units())


def test_empty_ingredient_item_raises(pack) -> None:
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 10\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(ParseError, match="item is empty"):
        parse_source_md(md, known_units=pack.known_units())


def test_f2_parse_requires_known_units() -> None:
    """WP-F2: nessun default silenzioso, le unita' vengono solo dal pack."""
    md = (
        "---\ntitle: X\nid: RIC-X\nlang: it\nservings: 2\ntime_min: 5\n"
        "difficulty: facile\n---\n## Ingredienti\n- 1 g sale\n"
        "## Procedimento\n1. x\n"
    )
    with pytest.raises(TypeError, match="known_units"):
        parse_source_md(md)


# ---------------------------------------------------------------------------
# WP-F3 — grammatica delle quantita'
# ---------------------------------------------------------------------------

# (riga ingrediente, qty, qty_max, to_taste, unit, item)
QUANTITY_CASES = [
    # interi e decimali
    ("200 g farina", "200", None, False, "g", "farina"),
    ("1.5 kg asparagi", "1.5", None, False, "kg", "asparagi"),
    ("1,5 dl latte", "1.5", None, False, "dl", "latte"),
    ("0,5 l brodo", "0.5", None, False, "l", "brodo"),
    # frazioni tipografiche
    ("½ cipolla", "0.5", None, False, None, "cipolla"),
    ("¼ l vino", "0.25", None, False, "l", "vino"),
    ("¾ tazza zucchero", "0.75", None, False, "tazza", "zucchero"),
    ("⅓ l panna", "0.333", None, False, "l", "panna"),
    # miste e ascii
    ("1 ½ cucchiaio senape", "1.5", None, False, "cucchiaio", "senape"),
    ("2 ½ dl vino", "2.5", None, False, "dl", "vino"),
    ("1/2 cipolla", "0.5", None, False, None, "cipolla"),
    ("3/4 l latte", "0.75", None, False, "l", "latte"),
    # intervalli
    ("2-3 uova", "2", "3", False, None, "uova"),
    ("2–3 uova", "2", "3", False, None, "uova"),
    ("10 - 12 g sale", "10", "12", False, "g", "sale"),
    ("½-1 cucchiaino zucchero", "0.5", "1", False, "cucchiaino", "zucchero"),
    # q.b. in testa e in coda, italiano e inglese
    ("q.b. sale", None, None, True, None, "sale"),
    ("qb sale", None, None, True, None, "sale"),
    ("q.b. sale e pepe", None, None, True, None, "sale e pepe"),
    ("sale q.b.", None, None, True, None, "sale"),
    ("a piacere pepe", None, None, True, None, "pepe"),
    ("to taste salt", None, None, True, None, "salt"),
    ("salt to taste", None, None, True, None, "salt"),
    # riga senza dose: e' "a piacere", non un errore
    ("sale", None, None, True, None, "sale"),
    ("olio per friggere", None, None, True, None, "olio per friggere"),
    # articolo in testa: non fa parte del nome
    ("il succo di 1 limone", None, None, True, None, "succo di 1 limone"),
    ("1 la cipolla", "1", None, False, None, "cipolla"),
    ("l’uovo", None, None, True, None, "uovo"),
    # unita' dopo una frazione
    ("½ cucchiaino senape", "0.5", None, False, "cucchiaino", "senape"),
    # il pizzico vero resta un pizzico
    ("1 pizzico noce moscata", "1", None, False, "pizzico", "noce moscata"),
]


@pytest.mark.parametrize(
    ("line", "qty", "qty_max", "to_taste", "unit", "item"), QUANTITY_CASES
)
def test_f3_quantity_grammar(pack, line, qty, qty_max, to_taste, unit, item) -> None:
    parsed = _parse_ingredient(
        line, 1, pack.known_units(), pack.countable_units()
    )
    assert parsed.qty == qty, line
    assert parsed.qty_max == qty_max, line
    assert parsed.to_taste is to_taste, line
    assert parsed.unit == unit, line
    assert parsed.item == item, line


@pytest.mark.parametrize(("line", "qty", "qty_max", "to_taste", "unit", "item"), QUANTITY_CASES)
def test_f3_render_is_the_inverse_of_parse(
    pack, line, qty, qty_max, to_taste, unit, item
) -> None:
    """Riparsare la riga resa restituisce gli stessi campi (T9 dipende da questo)."""
    parsed = _parse_ingredient(
        line, 1, pack.known_units(), pack.countable_units()
    )
    rendered = render_ingredient_line(parsed)
    assert rendered.startswith("- ")
    reparsed = _parse_ingredient(
        rendered[2:], 1, pack.known_units(), pack.countable_units()
    )
    assert (reparsed.qty, reparsed.qty_max, reparsed.to_taste,
            reparsed.unit, reparsed.item) == (
        parsed.qty, parsed.qty_max, parsed.to_taste, parsed.unit, parsed.item
    ), rendered


def test_f3_to_taste_renders_in_the_target_language(pack) -> None:
    parsed = _parse_ingredient("q.b. sale", 1, pack.known_units())
    assert render_ingredient_line(parsed) == "- to taste sale"
    assert (
        render_ingredient_line(parsed, to_taste_text=TO_TASTE_IT)
        == "- q.b. sale"
    )


def test_f3_suffix_survives_the_new_grammar(pack) -> None:
    """Il suffisso strutturale non entra nell'item nemmeno senza quantita'."""
    parsed = _parse_ingredient(
        "q.b. salt {code: CM00591, waste: 2%}", 1, pack.known_units()
    )
    assert parsed.item == "salt"
    assert parsed.code == "CM00591"
    assert parsed.waste == "2%"
    assert parsed.qty is None
    assert (
        render_ingredient_line(parsed)
        == "- to taste salt {code: CM00591, waste: 2%}"
    )
