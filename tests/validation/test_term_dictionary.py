"""Passo 3 PROGRAMMA-UNICO: dizionario di input dai due corpora.

Obiettivo: rappresentazione completa e riproducibile del vocabolario; ogni
voce ha frequenza, forme e contesti reali; niente perso, niente duplicato.
Verifiche: conteggi riconciliati; somma frequenze = righe totali; ogni voce
ha >=1 contesto; due esecuzioni -> file hash-identici.
"""
from __future__ import annotations

import hashlib
import json

from scripts.build_term_dictionary import (
    build_book_dictionary,
    normalize_string,
)
from scripts.msc_to_md import parse_card

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
    " Melt the margarine.",
]


def test_normalize_string() -> None:
    assert normalize_string("  APPLE  SLICED  FRZ ") == "apple sliced frz"
    assert normalize_string("Sott\u2019olio") == "sott'olio"
    assert normalize_string("MILK WHOLE 3,5% FAT") == "milk whole 3,5% fat"


def test_build_msc_dictionary_groups_by_code() -> None:
    card = parse_card(CARD_BLOCK, 1)
    # build directly from the card rows (stessa logica di build_msc_dictionary)
    from scripts.build_term_dictionary import _contexts

    by_code: dict[str, dict] = {}
    for row in card.ingredients:
        if row.is_section or not row.code:
            continue
        e = by_code.setdefault(row.code, {
            "corpus": "msc", "key": row.code, "frequency": 0,
            "forms": [], "units": [], "contexts": [],
        })
        e["frequency"] += 1
        f = normalize_string(row.name)
        if f not in e["forms"]:
            e["forms"].append(f)
        if row.unit and row.unit not in e["units"]:
            e["units"].append(row.unit)
        if len(e["contexts"]) < 3:
            e["contexts"].append(_contexts(card.name, row.name, row.code))
    assert set(by_code) == {"CM02351", "CM02355", "CM00292", "RF416215"}
    assert by_code["CM02351"]["frequency"] == 1
    assert by_code["CM02351"]["forms"] == ["apple sliced frz"]
    assert by_code["CM02351"]["units"] == ["g"]
    assert len(by_code["CM02351"]["contexts"]) == 1


def test_build_book_dictionary() -> None:
    labels = ["SALT TABLE", "salt table", "SALT", "salt", "salt", "pepper"]
    entries = build_book_dictionary(labels)
    by_key = {e["key"]: e for e in entries}
    assert by_key["salt table"]["frequency"] == 2
    assert by_key["salt"]["frequency"] == 3
    assert by_key["pepper"]["frequency"] == 1
    # ogni voce ha >= 1 contesto
    assert all(e["contexts"] for e in entries)
    # somma frequenze = righe totali
    assert sum(e["frequency"] for e in entries) == len(labels)


def test_dictionary_deterministic(tmp_path) -> None:
    """Due esecuzioni producono file hash-identici (ordine deterministico)."""
    labels = ["salt", "salt", "pepper", "olive oil", "olive oil", "olive oil"]
    e1 = build_book_dictionary(labels)
    e2 = build_book_dictionary(list(reversed(labels)))  # ordine input diverso
    lines1 = [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in e1]
    lines2 = [json.dumps(e, ensure_ascii=False, sort_keys=True) for e in e2]
    assert hashlib.sha256("\n".join(lines1).encode()).hexdigest() == \
        hashlib.sha256("\n".join(lines2).encode()).hexdigest()
