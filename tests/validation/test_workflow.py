"""Test del workflow di ricerca e validazione ricette (branch validate-recipe)."""
from __future__ import annotations

import pathlib

import pytest

from app.auth import Principal
from app.domain import load_domain_pack
from app.storage.client import Neo4jClient
from app.validation.ingest import detect_format, detect_language, read_recipes, split_subrecipes
from app.validation.workflow import run_validation_workflow
from tests.agents.conftest import PACK_DIR

PREFIX = "ivw_"

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


def test_detect_language_and_format() -> None:
    assert detect_language("## Ingredienti\n- 1 kg farina") == "it"
    assert detect_language("## Ingredients\n- 1 kg flour") == "en"
    assert detect_format(pathlib.Path("x.pdf")) == "calcmenu"
    assert detect_format(pathlib.Path("x.md"), "## Ingredienti") == "md_source"
    assert detect_format(pathlib.Path("x.md"), "## Ingredients") == "md_translated"


def test_split_subrecipes() -> None:
    from app.validation.ingest import RawRecipe
    r = RawRecipe(source="t", name="Main", code="RF1", servings=10,
                  ingredients=[
                      {"code": "CM001", "name": "flour", "qty": 100, "unit": "g", "prep": None},
                      {"code": "RF200", "name": "Bechamel", "qty": 1, "unit": "l", "prep": None},
                      {"code": "SF300", "name": "Tomato sauce", "qty": 2, "unit": "l", "prep": None},
                  ], procedure=["1. Mix."])
    main, subs = split_subrecipes(r)
    assert len(subs) == 2
    assert subs[0].name == "Bechamel" and subs[0].code == "RF200"
    assert subs[1].name == "Tomato sauce" and subs[1].code == "SF300"
    assert len(main.ingredients) == 3  # le righe restano come riferimento


def test_read_recipes_md(tmp_path) -> None:
    f = tmp_path / "ricetta.md"
    f.write_text(SAMPLE_MD, encoding="utf-8")
    recipes = read_recipes(f)
    assert len(recipes) == 1
    assert recipes[0].name == "Asparagus with butter"
    assert recipes[0].servings == 4
    assert recipes[0].language == "en"
    assert len(recipes[0].ingredients) == 4


async def test_workflow_found_and_not_found(client: Neo4jClient, pack_dir, tmp_path) -> None:
    """Workflow: la ricetta presente nel knowledge viene validata; una assente -> NON PRESENTE."""
    from scripts.load_domain_pack import load_pack
    pack = load_domain_pack(str(pack_dir))
    load_pack(client, pack_dir)
    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH 'ivw_' DETACH DELETE n")
        s.run("DROP INDEX document_embedding_vector IF EXISTS")
        s.run("CREATE VECTOR INDEX document_embedding_vector IF NOT EXISTS FOR (d:Document) ON (d.embedding) OPTIONS {indexConfig: {`vector.dimensions`: 384, `vector.similarity_function`: 'cosine'}}")

    # ingesta la ricetta di riferimento nel knowledge
    from app.validation.validator import validate_and_ingest
    validate_and_ingest(client, pack, SAMPLE_MD, source_ref={"author": "T", "book": "B", "page": "1", "position": "p"}, prefix=PREFIX)

    # dir con 2 ricette: una presente, una assente
    d = tmp_path / "to_validate"
    d.mkdir()
    (d / "present.md").write_text(SAMPLE_MD, encoding="utf-8")
    (d / "absent.md").write_text(
        SAMPLE_MD.replace("Asparagus with butter", "Zucchini with chocolate")
                .replace("asparagus", "zucchini").replace("Grana Padano", "chocolate"),
        encoding="utf-8",
    )

    principal = Principal(f"{PREFIX}u_admin", ("admin",), (), "default", f"{PREFIX}j_admin")
    report = await run_validation_workflow(d, pack, client, principal, out_dir=tmp_path / "out")

    assert report.total == 2
    assert report.found >= 1
    assert report.not_found >= 1
    # report globale scritto
    assert (tmp_path / "out" / "validation_report.json").exists()
    # ricette standardizzate scritte
    assert len(list((tmp_path / "out").glob("*.md"))) >= 2

    with client.session() as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH 'ivw_' DETACH DELETE n")
