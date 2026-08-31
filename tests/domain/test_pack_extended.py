"""Passo 2 PROGRAMMA-UNICO: schema pack esteso + unita' di conteggio.

Obiettivo: estendere senza rompere (dominio code intatto); le unita' di
conteggio sono riconosciute dal parser ma nessuno stadio le converte o
riscrive.
"""
from __future__ import annotations

import pathlib

import pytest
from pydantic import ValidationError

from app.domain import canonicalize, load_domain_pack, parse_translated_md
from app.domain.pack import GlossaryEntry

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"
CODE_PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "code"


def _pack():
    return load_domain_pack(str(PACK_DIR))


# ---------------------------------------------------------------------------
# unita' di conteggio riconosciute dal parser
# ---------------------------------------------------------------------------

def test_known_units_include_count_and_industrial() -> None:
    units = _pack().known_units()
    for u in ("cl", "lt", "mg", "ea", "pz", "serving", "servings", "egg", "eggs",
              "KG", "LT", "EA", "TT", "PZ"):
        assert u in units, f"unita' {u!r} mancante da known_units()"


def test_parser_recognizes_count_units() -> None:
    pack = _pack()
    md = """---
title: Test
id: T-1
lang: en
source_lang: en
servings: 10
---
## Ingredients
- 2 egg whites
- 3 EA bread buns
- 10 pz cherry tomatoes
- 150 cl oil
- 3 LT stock
- 10 mg thyme
- 0 TT salt
## Method
1. Mix.
"""
    parsed = parse_translated_md(
        md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
    )
    units = [i.unit for i in parsed.ingredients]
    assert units == [None, "EA", "pz", "cl", "LT", "mg", "TT"]
    # "egg whites" e' un composto (item unico); l'item non e' inquinato
    assert [i.item for i in parsed.ingredients] == [
        "egg whites", "bread buns", "cherry tomatoes", "oil", "stock", "thyme", "salt",
    ]


def test_canonicalize_noop_on_count_units() -> None:
    """canonicalize non converte/riscrive le unita' di conteggio (no-op)."""
    pack = _pack()
    md = """---
title: Test
id: T-2
lang: en
source_lang: en
servings: 10
---
## Ingredients
- 2 egg whites
- 3 EA bread buns
- 10 pz cherry tomatoes
## Method
1. Mix.
"""
    doc = canonicalize(pack, md)
    assert "- 2 egg whites" in doc.canonical_md
    assert "- 3 EA bread buns" in doc.canonical_md
    assert "- 10 pz cherry tomatoes" in doc.canonical_md
    # nessuna entry di canon-log per le righe conteggio (no-op reale)
    assert not any("ingredients[" in e.field for e in doc.log_entries)


# ---------------------------------------------------------------------------
# schema pack esteso
# ---------------------------------------------------------------------------

def test_glossary_entry_extended_fields() -> None:
    e = GlossaryEntry(
        id="ING-EGG", labels_en="egg", labels_it="uovo",
        **{"class": "uovo"},
        allergen_tags=["eggs"],
        countable_unit="egg", unit_weight_g=50, count_policy="integer",
        density_g_per_ml=None, ambiguous=False,
    )
    assert e.class_ == "uovo"
    assert e.allergen_tags == ["eggs"]
    assert e.unit_weight_g == 50.0
    assert e.count_policy == "integer"


def test_countable_requires_weight_and_policy() -> None:
    with pytest.raises(ValidationError):
        GlossaryEntry(id="X", labels_en="egg", labels_it="uovo", countable_unit="egg")
    with pytest.raises(ValidationError):
        GlossaryEntry(id="X", labels_en="egg", labels_it="uovo",
                      countable_unit="egg", unit_weight_g=50)


def test_class_enum_closed() -> None:
    with pytest.raises(ValidationError):
        GlossaryEntry(id="X", labels_en="x", labels_it="y", **{"class": "pietra"})


def test_allergens_eu_fic() -> None:
    with pytest.raises(ValidationError):
        GlossaryEntry(id="X", labels_en="x", labels_it="y", allergen_tags=["pomodoro"])


def test_code_pack_loads_unchanged() -> None:
    """Il dominio code (iter. D) carica invariato con lo schema esteso."""
    pack = load_domain_pack(str(CODE_PACK_DIR))
    assert pack.pack.name == "code"
    assert pack.glossary_entries()


def test_plausibilita_rules_loaded() -> None:
    pack = _pack()
    rules = pack.rules["plausibilita"]
    assert "per_portion_grams" in rules
    assert "proteina" in rules["per_portion_grams"]
    assert "zero_qty_a_piacere_classes" in rules["rules"]
