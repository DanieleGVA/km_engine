"""WP-C1 analyst tests: brief structure, determinism and versioned artifact."""
from __future__ import annotations

import json

from app.agents import analyze_corpus, clean_item, load_brief, write_brief
from app.agents.models import DomainBrief
from tests.agents.conftest import BRIEF_DIR, pilot_corpus


def test_ic_analyst_brief_structure(pilot_brief: DomainBrief) -> None:
    """The brief carries entities, vocabularies, units, ambiguities, ontologies."""
    assert pilot_brief.domain == "ricette"
    assert pilot_brief.language == "it"
    assert pilot_brief.canonical_language == "en"
    assert pilot_brief.corpus_size == 15

    names = {vocabulary.name for vocabulary in pilot_brief.vocabularies}
    assert names == {"tecnica", "ingredienti", "stati"}

    ingredients = pilot_brief.vocabulary("ingredienti")
    assert ingredients is not None
    assert len(ingredients.entries) >= 40
    assert all(entity.kind == "ingredient" for entity in ingredients.entries)
    assert all(entity.frequency >= 1 for entity in ingredients.entries)

    # Every ingredient entity has at least one Italian surface form.
    assert all(entity.source_terms for entity in ingredients.entries)

    # Units are detected with frequencies and examples.
    unit_names = {unit.unit for unit in pilot_brief.units}
    assert {"g", "kg", "ml", "l", "cucchiai", "pizzico"} <= unit_names

    # P7 ontology candidates are present.
    prefixes = {ontology.prefix for ontology in pilot_brief.ontologies}
    assert {"foodon", "dbpedia"} <= prefixes


async def test_ic_analyst_deterministic() -> None:
    """Two analyses of the same corpus produce the same brief (modulo timestamp)."""
    from app.agents import translate_corpus
    from app.domain import load_domain_pack
    from tests.agents.conftest import PACK_DIR
    from tests.domain.fake_llm import build_fake_llm

    pack = load_domain_pack(PACK_DIR)
    corpus = pilot_corpus()
    llm = build_fake_llm(pack, corpus)

    first = analyze_corpus(
        corpus,
        await translate_corpus(pack, corpus, llm),
        known_units=pack.known_units(),
        countable_units=pack.countable_units(),
    )
    second = analyze_corpus(
        corpus,
        await translate_corpus(pack, corpus, llm),
        known_units=pack.known_units(),
        countable_units=pack.countable_units(),
    )

    first_dump = first.model_dump(mode="json")
    second_dump = second.model_dump(mode="json")
    first_dump.pop("generated_at")
    second_dump.pop("generated_at")
    assert first_dump == second_dump


def test_ic_analyst_write_and_load_brief(tmp_path, pilot_brief: DomainBrief) -> None:
    """The brief round-trips through its versioned JSON artifact."""
    path = write_brief(pilot_brief, tmp_path / "ricette-v1.json")
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["domain"] == "ricette"
    assert payload["schema_version"] == "1.0"

    restored = load_brief(path)
    assert restored == pilot_brief


def test_ic_analyst_clean_item() -> None:
    """WP-F1: ``clean_item`` e' ``normalize_key`` + rimozione parentesi.

    Il connettore INTERNO non viene piu' rimosso: ``sale e pepe`` e
    ``olio extravergine di oliva`` sono termini interi, e il brief deve
    proporli nella forma che il canonicalizzatore incontrera' davvero (D2).
    """
    assert clean_item("pollo intero (1.2 kg)") == "pollo intero"
    assert clean_item("di extra virgin olive oil") == "extra virgin olive oil"
    assert clean_item("sale e pepe") == "sale e pepe"
    assert clean_item("olio extravergine di oliva") == "olio extravergine di oliva"
    assert clean_item("2 spicchi d’aglio") == "2 spicchi aglio"
    assert clean_item("  Farina  00 ") == "farina 00"


def test_ic_analyst_default_brief_path() -> None:
    from app.agents import default_brief_path

    assert default_brief_path().name == "ricette-v1.json"
    assert default_brief_path().parent.resolve() == BRIEF_DIR.resolve()
