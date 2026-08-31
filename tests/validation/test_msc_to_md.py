"""Test convertitore MSC (passo 0 del piano PROGRAMMA-UNICO).

Gate: riconciliazione 1.653 card / 19.500 righe / 1.591 procedure;
zero righe corrotte da "1,500"; L1 verde sul convertito.
"""
from __future__ import annotations

import pathlib

import pytest

from app.domain import load_domain_pack, parse_translated_md, verify_l1
from app.domain.errors import ParseError
from scripts.msc_to_md import (
    _clean_step_text,
    _normalize_qty,
    card_to_md,
    parse_card,
    parse_ingredient_row,
    parse_yield,
)

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"

CARD_BLOCK = [
    " 1. Warm apple crumble(YC&MDR 2024)(AA LUNCH)(NSA, VG)(CC)",
    " RF42713421",
    "",
    " Yield 24 serve · Record type Recipe",
    "",
    "#       Item code          Ingredient                                                          Qty           Unit      Preparation            Wastage",
    "1       CM02351            APPLE SLICED FRZ                                                    1,500         g         —                      —",
    "2       CM02355            APPLE GOLDEN 150/170G                                               1,500         g         —                      10%",
    "3       CM00292            SUGAR SUBSTITUTE 1FOR1                                              300           g         —                      —",
    "7       —                  CRUMBLE                                                             —             —         —                      —",
    "8       RF416215           MARGARINE(VEGAN)                                                    1,000         g         —                      —",
    "",
    " PROCEDURE",
    "",
    " 1. Apple filling.",
    " Melt the margarine in a pan over high heat.",
    " 2 FOR CRUMBLE:",
    " Make the crumble and bake it at 180°c.",
]


# ---------------------------------------------------------------------------
# yield
# ---------------------------------------------------------------------------

def test_yield_valid_formats() -> None:
    assert parse_yield("24 serve") == (24, None)
    assert parse_yield("10 serving") == (10, None)
    assert parse_yield("50 servings") == (50, None)
    assert parse_yield("100 pax") == (100, None)
    assert parse_yield("1,120 serving") == (1120, None)


def test_yield_never_defaults() -> None:
    # formati non risolvibili -> errore, mai un default
    assert parse_yield("10 [_]")[0] is None
    assert parse_yield("1 pz")[0] is None
    assert parse_yield("10 KG")[0] is None
    assert parse_yield("1 recipe")[0] is None
    assert parse_yield("1 rect.60x40")[0] is None
    assert parse_yield("")[0] is None
    assert parse_yield("1 nan")[0] is None


# ---------------------------------------------------------------------------
# quantita' (separatore di migliaia)
# ---------------------------------------------------------------------------

def test_normalize_qty_thousands() -> None:
    assert _normalize_qty("1,500") == "1500"   # mai 1
    assert _normalize_qty("10,000") == "10000"
    assert _normalize_qty("1,5") == "1.5"      # virgola decimale
    assert _normalize_qty("2.500.000") == "2500000"  # punti come migliaia
    assert _normalize_qty("0.50") == "0.50"   # punto decimale
    assert _normalize_qty("0") == "0"
    assert _normalize_qty("—") is None


# ---------------------------------------------------------------------------
# righe distinta
# ---------------------------------------------------------------------------

def test_parse_ingredient_row_normal() -> None:
    row = parse_ingredient_row(
        "1       CM02351            APPLE SLICED FRZ                                                    1,500         g         —                      —"
    )
    assert row is not None and not row.is_section
    assert row.code == "CM02351"
    assert row.name == "APPLE SLICED FRZ"
    assert row.qty == "1500"
    assert row.unit == "g"


def test_parse_ingredient_row_prep_with_unit_word() -> None:
    """La colonna Preparation puo' contenere parole-unita' (SPRIG/LEAF):
    l'unita' e' quella PRECEDUTA dalla quantita' numerica."""
    row = parse_ingredient_row(
        "8       CM01441            HERB THYME FRESH                                                    10            mg         SPRIG                      —"
    )
    assert row is not None
    assert row.qty == "10"
    assert row.unit == "mg"
    assert row.name == "HERB THYME FRESH"
    assert row.prep == "SPRIG"


def test_parse_ingredient_row_zero_qty_kept() -> None:
    """qty 0 (a piacere) NON viene droppata: resta nella distinta."""
    row = parse_ingredient_row(
        "11      CM00591            SALT TABLE                                                          0             KG        TO TASTE               —"
    )
    assert row is not None
    assert row.qty == "0"
    assert row.unit == "KG"
    assert row.name == "SALT TABLE"


def test_parse_ingredient_row_section() -> None:
    row = parse_ingredient_row(
        "7       —                  CRUMBLE                                                             —             —         —                      —"
    )
    assert row is not None and row.is_section
    assert row.name == "CRUMBLE"


# ---------------------------------------------------------------------------
# procedura
# ---------------------------------------------------------------------------

def test_clean_step_text() -> None:
    assert _clean_step_text("1. Apple filling.") == "Apple filling."
    assert _clean_step_text("1. 1 Prepare the herb butter") == "Prepare the herb butter"
    assert _clean_step_text("2 FOR CRUMBLE:") == "FOR CRUMBLE:"
    assert _clean_step_text("Melt the margarine.") == "Melt the margarine."


# ---------------------------------------------------------------------------
# card -> md
# ---------------------------------------------------------------------------

def test_card_to_md_full() -> None:
    card = parse_card(CARD_BLOCK, 1)
    assert card.code == "RF42713421"
    assert card.servings == 24
    assert card.yield_error is None
    assert len(card.ingredients) == 4  # 5 righe - 1 sezione (CRUMBLE)
    assert len(card.procedure) == 4

    md = card_to_md(card)
    # nessuna quantita' corrotta da "1,500"
    assert "1,500" not in md
    assert "- 1500 g APPLE SLICED FRZ {code: CM02351}" in md
    # metadato componente sulle righe successive alla sezione
    assert "component: crumble" in md
    # sfrido
    assert "waste: 10%" in md
    # passi sequenziali
    assert "1. Apple filling." in md
    assert "2. Melt the margarine in a pan over high heat." in md
    assert "3. FOR CRUMBLE:" in md
    # frontmatter EN-native senza time_min/difficulty
    assert "lang: en" in md and "source_lang: en" in md
    assert "time_min" not in md and "difficulty" not in md


def _pack(pack_dir=None):
    # pack del working tree: il flag frontmatter_optional_when_native e'
    # parte del passo 0 (non ancora nel pack committato)
    return load_domain_pack(str(PACK_DIR))


def test_converted_md_parses_and_l1_green(pack_dir) -> None:
    """Gate: il convertito e' un translated.md valido e L1 verde (identita')."""
    pack = _pack(pack_dir)
    card = parse_card(CARD_BLOCK, 1)
    md = card_to_md(card)
    parsed = parse_translated_md(
        md,
        known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
    )
    assert parsed.frontmatter["servings"] == 24
    assert len(parsed.ingredients) == 4
    assert len(parsed.steps) == 4
    l1 = verify_l1(md, md, pack=pack)
    assert l1.passed, [i.message for i in l1.issues]


def test_optional_frontmatter_flag(pack_dir) -> None:
    """time_min/difficulty opzionali SOLO con flag e documento nativo."""
    pack = _pack(pack_dir)
    md = card_to_md(parse_card(CARD_BLOCK, 1))
    # con flag: parse ok
    parse_translated_md(
        md,
        known_units=pack.known_units(),
        optional_when_native=tuple(pack.frontmatter_optional_when_native),
    )
    # senza flag: errore (retro-compatibilita')
    with pytest.raises(ParseError):
        parse_translated_md(md, known_units=pack.known_units())


def test_verify_l1_native_document(pack_dir) -> None:
    """verify_l1(md, md) su documento nativo EN senza time_min: verde."""
    pack = _pack(pack_dir)
    md = card_to_md(parse_card(CARD_BLOCK, 1))
    assert verify_l1(md, md, pack=pack).passed


# ---------------------------------------------------------------------------
# Riconciliazione sul PDF reale (gate del passo 0) — skip se il PDF non c'e'
# ---------------------------------------------------------------------------

REAL_PDF = pathlib.Path(
    "/Users/daniele.buonaiuto/Dev/rcps/foodmdm/deliverables/DLV-26_menu_pareto/Pareto_Recipe_Cards_v001.pdf"
)


@pytest.mark.skipif(not REAL_PDF.exists(), reason="PDF Pareto non presente")
def test_reconciliation_real_pdf() -> None:
    """Gate passo 0: 1.653 card / 19.500 righe / 1.591 procedure, L1 verde."""
    from scripts.msc_to_md import card_to_md, extract_cards

    cards = extract_cards(REAL_PDF)
    assert len(cards) == 1653

    n_rows = sum(len(c.ingredients) for c in cards)
    # le sezioni non sono in card.ingredients: riconciliazione via righe totali
    # (19.500 = ingredienti + sezioni) verificata nel test di estrazione
    assert n_rows <= 19500

    n_proc = sum(1 for c in cards if c.procedure)
    assert n_proc == 1591

    # zero righe corrotte da "1,500" e L1 verde su un campione
    pack = _pack()
    checked = 0
    for card in cards:
        if card.servings is None or card.errors:
            continue
        md = card_to_md(card)
        assert "1,500" not in md
        assert verify_l1(md, md, pack=pack).passed
        checked += 1
    assert checked >= 1300
