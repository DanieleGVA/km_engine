"""Passo 9 PROGRAMMA-UNICO: verify_intra (screen 6 famiglie).

Obiettivo: le contraddizioni interne alla card emergono con regole esatte;
le convenzioni legittime non sono difetti.
Verifiche: casi seminati per ciascuna famiglia intercettati; "0 KG SALT
TABLE" NON flaggato (a piacere); "0 KG ONION" flaggato; citato ma
plausibilmente in sotto-ricetta => severita' cap medium.
"""
from __future__ import annotations

import pathlib

from app.domain import load_domain_pack, verify_intra

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"

MD = """---
title: Chicken with asparagus
id: T-9
lang: en
source_lang: en
servings: 4
---
## Ingredients
- 1 kg chicken
- 200 g asparagus
- 0 TT salt
- 0 kg onion
- 5 kg thyme
- 3 kg thyme
## Method
1. Cook the chicken with asparagus at 120°c.
2. Add the butter and mix.
3. Freeze at -20°c.
"""


def _pack():
    return load_domain_pack(str(PACK_DIR))


def test_six_families_detected() -> None:
    report = verify_intra(MD, _pack())
    families = {f.family for f in report.findings}
    # 1) citato-non-costato: butter citato, assente dalla distinta
    assert "cited_not_costed" in families
    # 2) titolo vs distinta: chicken/asparagus nel titolo e nella distinta;
    #    (nessun termine del titolo assente -> famiglia non attiva qui)
    # 3) mass balance: 0 kg onion anomalo
    assert "mass_balance" in families
    # 4) temperature: -20°c fuori range
    assert "temperature" in families
    # 5) integrita' unita': kg su erba (thyme) sospetto
    assert "unit_integrity" in families
    # 6) duplicati: thyme due volte
    assert "duplicates" in families


def test_zero_salt_a_piacere_not_flagged() -> None:
    """'0 TT salt' (a piacere) NON e' flaggato; '0 kg onion' SI'."""
    report = verify_intra(MD, _pack())
    zero = [f for f in report.findings if f.family == "mass_balance"]
    assert any("onion" in f.message for f in zero)
    assert not any("salt" in f.message for f in zero)


def test_cited_not_costed_severity_cap() -> None:
    """Citato ma plausibilmente in sotto-ricetta => severita' cap medium."""
    report = verify_intra(MD, _pack())
    cited = [f for f in report.findings if f.family == "cited_not_costed"]
    assert cited
    assert all(f.severity in ("low", "medium") for f in cited)


def test_temperature_high() -> None:
    report = verify_intra(MD, _pack())
    temps = [f for f in report.findings if f.family == "temperature"]
    assert any("-20" in f.message for f in temps)
    assert all(f.severity == "high" for f in temps)
