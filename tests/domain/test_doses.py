"""Test standardizzazione dosi (fix: la resa non si inventa mai).

Fix immediato dal piano PROGRAMMA-UNICO: ``servings`` mancante o 0 deve
produrre errore, mai un default silenzioso (prima: default 4).
"""
from __future__ import annotations

import pytest

from app.domain.doses import standardize_doses
from app.domain.errors import ParseError

SAMPLE = """---
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


def test_scale_to_10(pack) -> None:
    doses = standardize_doses(SAMPLE, pack, servings_target=10)
    assert doses.servings == 10
    assert abs(doses.scale_factor - 2.5) < 1e-9
    assert "- 3.75 kg asparagus" in doses.canonical_md
    assert "servings: 10" in doses.canonical_md


def test_zero_servings_raises(pack) -> None:
    """servings=0 non scala piu' con un default silenzioso: errore."""
    md = SAMPLE.replace("servings: 4", "servings: 0")
    with pytest.raises(ParseError):
        standardize_doses(md, pack, servings_target=10)


def test_missing_servings_raises(pack) -> None:
    """servings assente: errore (mai default 4)."""
    md = SAMPLE.replace("servings: 4\n", "")
    with pytest.raises(ParseError):
        standardize_doses(md, pack, servings_target=10)
