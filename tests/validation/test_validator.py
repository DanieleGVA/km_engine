"""Test ricerca e validazione ricette (branch validate-recipe)."""
from __future__ import annotations

import pytest

from app.auth import Principal
from app.domain import load_domain_pack
from app.storage.client import Neo4jClient
from app.validation.validator import (
    search_recipes,
    validate_and_ingest,
    validate_recipe_md,
)
from tests.agents.conftest import PACK_DIR

PREFIX = "ivl_"

# Ricetta in formato translated/canonical (EN), yield 4 -> dosi x10
SAMPLE_MD = """---
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
1. Clean the asparagus and remove the woody base.
2. Boil in salted water for 5 minutes.
3. Saut\u00e9 with butter for 4 minutes.
4. Sprinkle with cheese and serve.
"""

# Ricetta con unita' non riconosciuta (deve fallire il check MKS)
BAD_UNIT_MD = SAMPLE_MD.replace("- 1.5 kg asparagus", "- 1.5 blob asparagus")


def test_validate_recipe_md_ok() -> None:
    pack = load_domain_pack(str(PACK_DIR))
    report = validate_recipe_md(SAMPLE_MD, pack, servings_target=10)
    assert report.recipe_id == "RIC-101"
    assert report.servings == 10
    assert abs(report.scale_factor - 2.5) < 1e-9
    assert report.n_ingredients == 4
    assert report.n_steps >= 3
    assert report.units_ok
    assert report.unknown_units == []
    assert report.coverage > 0.5  # almeno meta' degli ingredienti risolti dal glossario


def test_validate_recipe_md_bad_unit() -> None:
    pack = load_domain_pack(str(PACK_DIR))
    report = validate_recipe_md(BAD_UNIT_MD, pack, servings_target=10)
    assert not report.units_ok
    assert "blob" in report.unknown_units


def test_validate_recipe_md_dose_scaling() -> None:
    pack = load_domain_pack(str(PACK_DIR))
    report = validate_recipe_md(SAMPLE_MD, pack, servings_target=10)
    # 1.5 kg x 2.5 = 3.75 kg; 40 g x 2.5 = 100 g; 1 pinch x 2.5 = 1.25 g
    assert report.scale_factor == 2.5


def test_validate_and_ingest_rag(client: Neo4jClient, pack_dir) -> None:
    """Validazione + ingestione + RAG retrieval della ricetta normalizzata."""
    from scripts.load_domain_pack import load_pack
    pack = load_domain_pack(str(pack_dir))
    load_pack(client, pack_dir)
    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH 'ivl_' DETACH DELETE n")
        s.run("DROP INDEX document_embedding_vector IF EXISTS")
        s.run("CREATE VECTOR INDEX document_embedding_vector IF NOT EXISTS FOR (d:Document) ON (d.embedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}")

    report = validate_and_ingest(
        client, pack, SAMPLE_MD,
        source_ref={"author": "Test", "book": "Sample", "page": "1", "position": "sample#1"},
        servings_target=10, prefix=PREFIX,
    )
    assert report.rag_found, f"ricetta non ritrovata dal RAG: top1={report.rag_top1}"
    assert report.rag_top1 == "RIC-101"
    assert report.source_ref is not None
    assert report.source_ref["author"] == "Test"

    # ricerca per query naturale
    admin = Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")
    hits = search_recipes(client, admin, "asparagus with butter", lang="en", limit=5)
    assert any(h["document_id"] == "RIC-101" for h in hits)

    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH 'ivl_' DETACH DELETE n")
