"""WP-F1 — ``normalize_key``: la chiave di lookup e' simmetrica.

Il difetto D2 non era il glossario ma l'asimmetria: l'item veniva ripulito dei
connettori, la chiave di glossario no. Qui si fissa il contratto della singola
funzione che entrambi i lati devono attraversare.
"""
from __future__ import annotations

import pytest

from app.domain.normalize import normalize_key, normalize_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # elisione: l'articolo eliso non fa parte del termine
        ("spicchio d'aglio", "spicchio aglio"),
        ("d'aglio", "aglio"),
        ("l'uovo", "uovo"),
        ("dell'olio", "olio"),
        ("un'acciuga", "acciuga"),
        # apostrofo tipografico -> ASCII, ma "sott'" non e' un'elisione:
        # fa parte del termine e resta
        ("sott’olio", "sott'olio"),
        ("acciughe sott’olio", "acciughe sott'olio"),
        # connettore iniziale: residuo della segmentazione unita'/item
        ("di extra virgin olive oil", "extra virgin olive oil"),
        ("di olio extravergine di oliva", "olio extravergine di oliva"),
        ("degli spinaci", "spinaci"),
        # connettore interno: fa parte del termine, resta
        ("olio extravergine di oliva", "olio extravergine di oliva"),
        ("salt e black pepper", "salt e black pepper"),
        ("sale e pepe", "sale e pepe"),
        # forma
        ("  Sale  E   Pepe ", "sale e pepe"),
        ("", ""),
    ],
)
def test_normalize_key_cases(raw: str, expected: str) -> None:
    assert normalize_key(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "spicchio d'aglio",
        "di di aglio",
        "olio extravergine di oliva",
        "sott’olio",
        "",
    ],
)
def test_normalize_key_is_idempotent(raw: str) -> None:
    once = normalize_key(raw)
    assert normalize_key(once) == once


def test_normalize_key_idempotent_on_whole_glossary(pack) -> None:
    """Idempotenza su ogni termine reale del pack, non solo sui casi scelti."""
    for entry in pack.glossary_entries():
        for term in (entry.labels_en, entry.labels_it, *entry.aliases):
            key = normalize_key(term)
            assert normalize_key(key) == key, term


def test_normalize_text_preserves_layout() -> None:
    """``normalize_text`` non collassa gli spazi: sostituisce dentro un testo."""
    text = "## Ingredienti\n- 2 spicchi d'aglio\n- 1 dl d’olio"
    out = normalize_text(text)
    assert out.splitlines() == [
        "## ingredienti",
        "- 2 spicchi aglio",
        "- 1 dl olio",
    ]


def test_normalize_key_refines_normalize_text() -> None:
    """Le due funzioni non divergono: la chiave e' il testo normalizzato+rifinito."""
    for raw in ("di olio extravergine di oliva", "  spicchio d'aglio  ", "L'Uovo"):
        assert normalize_key(raw) == normalize_key(normalize_text(raw))
