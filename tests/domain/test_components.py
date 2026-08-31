"""Passo 11 PROGRAMMA-UNICO: decomposizione canone in CanonComponent.

Obiettivo: il canone e' interrogabile per componente; ogni componente cita il
documento d'origine; la ricomposizione restituisce esattamente gli
ingredienti (nessun orfano, nessuna aggiunta).
"""
from __future__ import annotations

import pathlib

from app.domain import load_domain_pack
from app.domain.components import (
    CLASS_COMPONENT,
    decompose_document,
    verify_recomposition,
    write_components,
)
from app.domain.extract import extract_document

PACK_DIR = pathlib.Path(__file__).resolve().parents[2] / "domain-packs" / "ricette"
PREFIX = "ic11_"

MD = """---
title: Test
id: T-11
lang: en
source_lang: en
servings: 4
---
## Ingredients
- 1 kg chicken {code: CM00001, component: main}
- 200 g rice {code: CM00002, component: starch}
- 100 g peas {code: CM00003, component: vegetable}
- 50 g butter
## Method
1. Cook.
"""


def _pack():
    return load_domain_pack(str(PACK_DIR))


def test_decompose_uses_suffix_component() -> None:
    groups = decompose_document(MD, "T-11", _pack())
    labels = {g.label: g.ingredient_positions for g in groups}
    assert labels["main"] == [0]
    assert labels["starch"] == [1]
    assert labels["vegetable"] == [2]
    # butter senza suffisso -> classe (latticino o grasso, a seconda della
    # voce del dizionario: il canone di classe R9 la uniformera')
    butter_group = [g for g in groups if 3 in g.ingredient_positions]
    assert len(butter_group) == 1
    assert butter_group[0].label in ("dairy", "fat")


def test_class_component_map() -> None:
    assert CLASS_COMPONENT["proteina"] == "main protein"
    assert CLASS_COMPONENT["amido"] == "starch"
    assert CLASS_COMPONENT["condimento"] == "sauce"


def test_write_and_verify_recomposition(client) -> None:
    """Scrittura + verifica: nessun orfano, nessuna aggiunta."""
    pack = _pack()
    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=PREFIX)
    doc_id = f"{PREFIX}T-11"
    extract_document(client, None, doc_id, MD, pack)
    groups = decompose_document(MD, doc_id, pack)
    write_components(client, doc_id, groups)
    problems = verify_recomposition(client, doc_id, [0, 1, 2, 3])
    assert problems == []
    # ogni componente cita il documento d'origine
    with client.session() as s:
        rows = s.run(
            "MATCH (c:CanonComponent)-[:PART_OF_DOC]->(d:Document {id: $id}) "
            "RETURN count(c) AS n", id=doc_id).single()
        assert rows["n"] == len(groups)
    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=PREFIX)
