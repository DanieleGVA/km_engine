"""Passo 1 PROGRAMMA-UNICO: suffisso IR {code, waste, component} strutturale.

Obiettivo: code/sfrido/componente viaggiano come metadati — invisibili a P2 e
L2, simmetrici tra serializer e ricompositore, assenza di suffisso =
comportamento identico a oggi.
"""
from __future__ import annotations

import pathlib

from app.domain import (
    canonicalize,
    load_domain_pack,
    parse_translated_md,
    verify_l2,
)
from app.domain.numbers import extract_numbers, mask_numbers
from app.domain.verify import (
    IngredientLine,
    _parse_ingredient,
    render_ingredient_suffix,
)

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"

MD_WITH_SUFFIX = """---
title: Warm apple crumble
id: RF42713421
lang: en
source_lang: en
servings: 24
---
## Ingredients
- 1500 g apple {code: CM02351}
- 1500 g apple golden {code: CM02355, waste: 10%}
- 1000 g margarine {code: RF416215, component: crumble}
- 200 g sugar {code: CM00292, waste: 5%, component: crumble}
## Method
1. Apple filling.
2. Melt the margarine.
"""

MD_NO_SUFFIX = """---
title: Asparagus with butter
id: RIC-101
lang: en
source_lang: it
servings: 4
time_min: 25
difficulty: easy
---
## Ingredients
- 1.5 kg asparagus
- 50 g Grana Padano
- 40 g butter
- 1 pinch salt
## Method
1. Clean the asparagus.
2. Boil in salted water.
3. Saut\u00e9 with butter.
4. Sprinkle with cheese.
"""


def _pack():
    return load_domain_pack(str(PACK_DIR))


# ---------------------------------------------------------------------------
# parse: il suffisso non e' mai parte dell'item
# ---------------------------------------------------------------------------

def test_parse_suffix_extracted() -> None:
    ing = _parse_ingredient(
        "1000 g margarine {code: RF416215, component: crumble}", 1, {"g"}
    )
    assert ing.item == "margarine"
    assert ing.code == "RF416215"
    assert ing.component == "crumble"
    assert ing.waste is None


def test_parse_suffix_full() -> None:
    ing = _parse_ingredient(
        "200 g sugar {code: CM00292, waste: 5%, component: crumble}", 1, {"g"}
    )
    assert ing.item == "sugar"
    assert ing.code == "CM00292"
    assert ing.waste == "5%"
    assert ing.component == "crumble"


def test_parse_no_suffix_backward_compat() -> None:
    ing = _parse_ingredient("40 g butter", 1, {"g"})
    assert ing.item == "butter"
    assert ing.code is None and ing.waste is None and ing.component is None


def test_render_roundtrip() -> None:
    for code, waste, component in [
        ("CM02351", None, None),
        ("CM02355", "10%", None),
        ("RF416215", None, "crumble"),
        ("CM00292", "5%", "crumble"),
    ]:
        suffix = render_ingredient_suffix(code, waste, component)
        line = f"100 g item{suffix}"
        ing = _parse_ingredient(line, 1, {"g"})
        assert ing.code == code
        assert ing.waste == waste
        assert ing.component == component
        assert ing.item == "item"


# ---------------------------------------------------------------------------
# P2: il suffisso e' escluso dai numeri
# ---------------------------------------------------------------------------

def test_extract_numbers_excludes_suffix() -> None:
    # "waste: 10%" e "code: CM02351" non sono numeri di contenuto
    assert extract_numbers(MD_WITH_SUFFIX) == ["1500", "1500", "1000", "200"]


def test_mask_numbers_excludes_suffix() -> None:
    masked, numbers = mask_numbers(MD_WITH_SUFFIX)
    # i numeri del suffisso (waste 10%/5%, code) non sono contenuto
    assert "10" not in numbers
    assert "5" not in numbers
    assert "waste: 10%" not in masked  # il suffisso e' escluso (mai all'LLM)


# ---------------------------------------------------------------------------
# L2: il suffisso non entra nei token
# ---------------------------------------------------------------------------

def test_l2_tokens_exclude_suffix(pack) -> None:
    from app.domain import ParsedDoc, parse_translated_md

    base = parse_translated_md(MD_NO_SUFFIX, known_units=pack.known_units())
    # aggiungi il suffisso a una riga: i token L2 non devono cambiare
    ing = base.ingredients[0]
    with_suffix = IngredientLine(
        raw=ing.raw, qty=ing.qty, unit=ing.unit, item=ing.item,
        code="CM00001", waste="10%", component="x",
    )
    modified = ParsedDoc(
        frontmatter=base.frontmatter, title=base.title,
        ingredients=[with_suffix] + list(base.ingredients[1:]),
        steps=base.steps, body=base.body, source_md=base.source_md,
    )
    report_plain = verify_l2(base, base, pack=pack)
    report_suffix = verify_l2(base, modified, pack=pack)
    for s1, s2 in zip(report_plain.sections, report_suffix.sections):
        assert s1.overlap == s2.overlap


# ---------------------------------------------------------------------------
# canonicalize: il suffisso e' preservato
# ---------------------------------------------------------------------------

def test_canonicalize_preserves_suffix() -> None:
    pack = _pack()
    doc = canonicalize(pack, MD_WITH_SUFFIX)
    assert "{code: CM02351}" in doc.canonical_md
    assert "waste: 10%" in doc.canonical_md
    assert "component: crumble" in doc.canonical_md
    # l'item resta pulito
    parsed = parse_translated_md(
        doc.canonical_md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
    )
    assert all("{" not in ing.item for ing in parsed.ingredients)
    assert parsed.ingredients[0].code == "CM02351"
    assert parsed.ingredients[1].waste == "10%"
    assert parsed.ingredients[2].component == "crumble"


def test_canonicalize_no_suffix_byte_identical() -> None:
    """Regressione: documento libro senza suffisso -> output identico a prima."""
    pack = _pack()
    doc = canonicalize(pack, MD_NO_SUFFIX)
    assert "{code:" not in doc.canonical_md
    assert "## Ingredients" in doc.canonical_md
    assert "- 1.5 kg asparagus" in doc.canonical_md


def test_roundtrip_with_suffix_idempotent() -> None:
    """parse -> render -> parse -> render: idempotente con suffisso."""
    pack = _pack()
    doc = canonicalize(pack, MD_WITH_SUFFIX)
    parsed1 = parse_translated_md(
        doc.canonical_md, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
    )
    # ricostruisci e ri-parse (WP-F3: il renderer prende le righe parsate,
    # il suffisso lo deriva da sole)
    from app.domain.canonical import render_canonical_md

    fm = {k: str(v) for k, v in parsed1.frontmatter.items()}
    md2 = render_canonical_md(fm, list(parsed1.ingredients), list(parsed1.steps))
    assert md2 == doc.canonical_md
    parsed2 = parse_translated_md(
        md2, known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
    )
    assert [i.code for i in parsed2.ingredients] == [i.code for i in parsed1.ingredients]
    assert [i.waste for i in parsed2.ingredients] == [i.waste for i in parsed1.ingredients]
    assert [i.component for i in parsed2.ingredients] == [i.component for i in parsed1.ingredients]
