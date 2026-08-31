"""Passo 5 PROGRAMMA-UNICO: standardizzazione batch + consolidazione.

Obiettivo: una proposta tracciabile per ogni voce, costruita solo dal
contesto; i divieti (mai fondere item, mai inventare specie) valgono piu'
della completezza. Collisioni e SAME_AS come proposte, mai applicati.
"""
from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from app.domain.llm import FakeLLMClient
from app.domain.standardize import (
    SYSTEM_PROMPT,
    DictionaryEntryProposal,
    build_batch_prompt,
    consolidate,
    standardize_batch,
)


def _run(coro):
    return asyncio.run(coro)


def _fake(entries: list[dict], batch: list[dict]) -> FakeLLMClient:
    return FakeLLMClient(judgements={
        (SYSTEM_PROMPT, build_batch_prompt(batch)): {"entries": entries},
    })


def test_fake_100_percent_schema_valid() -> None:
    """Con il fake, ogni proposta valida lo schema (gate: fake 100% valid)."""
    batch = [
        {"key": "CM00591", "corpus": "msc", "forms": ["SALT TABLE"], "units": ["g"], "contexts": ["x"]},
        {"key": "salt", "corpus": "book", "forms": ["salt"], "units": ["g"], "contexts": ["y"]},
    ]
    fake = _fake([
        {"key": "CM00591", "corpus": "msc", "canonical_name_en": "salt",
         "ingredient_core": "salt", "class": "condimento", "confidence": 0.9},
        {"key": "salt", "corpus": "book", "canonical_name_en": "salt",
         "ingredient_core": "salt", "class": "condimento", "confidence": 0.9},
    ], batch)
    proposals = _run(standardize_batch(fake, batch))
    assert len(proposals) == 2
    for p in proposals:
        DictionaryEntryProposal.model_validate(p)


def test_cheese_grated_ambiguous() -> None:
    """Caso seminato: 'CHEESE GRATED' -> core cheese, ambiguous=true."""
    batch = [{"key": "CM00001", "corpus": "msc", "forms": ["CHEESE GRATED"],
              "units": ["g"], "contexts": ["x"]}]
    fake = _fake([
        {"key": "CM00001", "corpus": "msc", "canonical_name_en": "cheese",
         "ingredient_core": "cheese", "class": "latticino",
         "ambiguous": True, "confidence": 0.6},
    ], batch)
    proposals = _run(standardize_batch(fake, batch))
    assert proposals[0].ingredient_core == "cheese"
    assert proposals[0].ambiguous is True


def test_two_butter_items_same_core() -> None:
    """Due item burro con pack diverso -> due voci, stesso core (mai fuse)."""
    batch = [
        {"key": "CM00010", "corpus": "msc", "forms": ["BUTTER UNSALTED 5KG"],
         "units": ["g"], "contexts": ["x"]},
        {"key": "CM00011", "corpus": "msc", "forms": ["BUTTER SALTED"],
         "units": ["g"], "contexts": ["y"]},
    ]
    fake = _fake([
        {"key": "CM00010", "corpus": "msc", "canonical_name_en": "unsalted butter",
         "ingredient_core": "butter", "states": ["unsalted"], "class": "latticino"},
        {"key": "CM00011", "corpus": "msc", "canonical_name_en": "salted butter",
         "ingredient_core": "butter", "states": ["salted"], "class": "latticino"},
    ], batch)
    proposals = _run(standardize_batch(fake, batch))
    assert len(proposals) == 2
    assert {p.ingredient_core for p in proposals} == {"butter"}


def test_consolidation_collisions_and_same_as() -> None:
    """Collisioni e SAME_AS compaiono come proposte, mai applicate."""
    proposals = [
        DictionaryEntryProposal(key="CM00010", corpus="msc",
                                canonical_name_en="unsalted butter",
                                ingredient_core="butter", states=["unsalted"],
                                class_="latticino"),
        DictionaryEntryProposal(key="CM00011", corpus="msc",
                                canonical_name_en="salted butter",
                                ingredient_core="butter", states=["salted"],
                                class_="latticino"),
        DictionaryEntryProposal(key="butter", corpus="book",
                                canonical_name_en="butter",
                                ingredient_core="butter", states=["unsalted"],
                                class_="latticino"),
    ]
    report = consolidate(proposals)
    # collisione: due voci MSC stesso (core, states) -> proposta di merge
    assert any(c["core"] == "butter" and len(c["keys"]) == 2 for c in report.collisions)
    # SAME_AS: MSC <-> libro stesso (core, states)
    assert any(s["msc"] == "CM00010" and s["book"] == "butter" for s in report.same_as)
    # nessuna proposta applicata: il report non modifica le voci
    assert len(report.proposals) == 3


def test_consolidation_rejects_invalid_class() -> None:
    """Nessuna voce con class fuori enum sopravvive alla consolidazione."""
    # la costruzione con class fuori enum fallisce (schema)
    with pytest.raises(ValidationError):
        DictionaryEntryProposal(key="x", corpus="msc",
                                canonical_name_en="x", ingredient_core="x",
                                **{"class": "pietra"})
    # e model_validate scarta le voci non valide
    bad = {"key": "x", "corpus": "msc", "canonical_name_en": "x",
           "ingredient_core": "x", "class": "pietra"}
    with pytest.raises(ValidationError):
        DictionaryEntryProposal.model_validate(bad)


def test_deterministic_same_input_same_output() -> None:
    """Stesso input + stesso fake -> stesso output (ordine deterministico)."""
    batch = [{"key": "k1", "corpus": "msc", "forms": ["A"], "units": ["g"], "contexts": ["c"]}]
    fixture = [{"key": "k1", "corpus": "msc", "canonical_name_en": "a",
                "ingredient_core": "a", "class": "altro"}]
    r1 = _run(standardize_batch(_fake(fixture, batch), batch))
    r2 = _run(standardize_batch(_fake(fixture, batch), batch))
    assert [p.model_dump() for p in r1] == [p.model_dump() for p in r2]
