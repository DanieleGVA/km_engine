"""Passo 8 PROGRAMMA-UNICO: dosi tipizzate + orchestratore flow.py.

Obiettivo: unita' naturali intoccabili ("2 uova" restano 2 uova); grammi
equivalenti solo nei log; nessuna stima silenziosa; resa mai inventata;
flow.py e' l'unico punto in cui translate->verify->canonicalize->doses e'
cablato.
"""
from __future__ import annotations

import pytest

from app.domain import load_domain_pack
from app.domain.doses import standardize_doses
from app.domain.errors import ParseError
from app.domain.flow import process_document
from tests.domain.conftest import PACK_DIR, read_corpus
from tests.domain.fake_llm import build_fake_llm

MD = """---
title: Test
id: T-8
lang: en
source_lang: en
servings: 4
---
## Ingredients
- 2 egg
- 3 leaf bay laurel
- 1.5 kg asparagus
- 40 g butter
- 220 kg salt
- 100 mg thyme
- 0 TT salt
## Method
1. Mix.
"""


def _pack():
    return load_domain_pack(str(PACK_DIR))


def test_natural_units_untouched() -> None:
    """'2 egg' e '3 leaf' restano invariate nel canonico dopo le dosi."""
    pack = _pack()
    doses = standardize_doses(MD, pack, servings_target=10)
    assert "- 5 egg" in doses.canonical_md          # 2 x 2.5
    assert "- 7.5 leaf bay laurel" in doses.canonical_md  # 3 x 2.5
    # grammi equivalenti SOLO nel log
    mass_entries = [e for e in doses.log_entries if e.rule_id == "DOSE-MASS-G"]
    assert any("egg" in e.after for e in mass_entries)
    assert any("leaf" in e.after for e in mass_entries)


def test_mass_g_priority_dictionary_over_generic() -> None:
    """mass_g: peso da dizionario > fattore generico; assente => issue."""
    pack = _pack()
    doses = standardize_doses(MD, pack, servings_target=10)
    # egg: peso generico 50 g (nessuna voce dizionario con unit_weight_g)
    egg_mass = [e for e in doses.log_entries
                if e.rule_id == "DOSE-MASS-G" and "egg" in e.after]
    assert egg_mass and float(egg_mass[0].mass_g) == 5 * 50.0


def test_count_policy_integer_rounds_up() -> None:
    """Scala x1,25 su policy integer => arrotondamento su, minimo 1."""
    pack = _pack()
    md = MD.replace("servings: 4", "servings: 8").replace("- 2 egg", "- 2 egg")
    doses = standardize_doses(md, pack, servings_target=10)
    # 2 egg x 1.25 = 2.5 -> integer => 3
    assert "- 3 egg" in doses.canonical_md


def test_servings_missing_raises() -> None:
    pack = _pack()
    md = MD.replace("servings: 4\n", "")
    with pytest.raises(ParseError):
        standardize_doses(md, pack, servings_target=10)


def test_plausibility_gate_blocks_220kg_and_100mg() -> None:
    """220 KG e 100 mg producono issue di plausibilita', non documenti puliti."""
    pack = _pack()
    doses = standardize_doses(MD, pack, servings_target=10)
    plaus = [i for i in doses.issues if "plausibilita'" in i]
    assert any("salt" in i for i in plaus)   # 220 kg sale
    assert any("thyme" in i for i in plaus)  # 100 mg timo


def test_a_piacere_zero_kept() -> None:
    """'0 TT salt' (a piacere) resta invariata, nessuno scaling."""
    pack = _pack()
    doses = standardize_doses(MD, pack, servings_target=10)
    assert "- 0 TT salt" in doses.canonical_md
    assert any(e.rule_id == "DOSE-A-PIACERE" for e in doses.log_entries)


def test_flow_process_document() -> None:
    """flow.py: sequenza completa translate->L1->L2->canonicalize->doses."""
    pack = _pack()
    corpus = read_corpus()
    llm = build_fake_llm(pack, corpus)
    import asyncio

    doc = asyncio.run(process_document(
        pack, corpus["ric-101-asparagi-burro.md"], llm, servings_target=10))
    assert doc.l1.passed
    assert doc.l2 is not None
    assert doc.doses.servings == 10
    assert doc.canonical.canonical_md
    # il flusso e' completo: doses parte dal canonical
    assert "## Ingredients" in doc.doses.canonical_md
