"""Test regole R1-R9 del dizionario (direttiva chef 31/08).

R1 quarantena non-ingredienti · R2 ri-segmentazione · R3 anti-fusione ·
R4 solfiti · R5 sedano · R6 glutine · R7 composizioni · R8 soia · R9 classe.
"""
from __future__ import annotations

from app.domain.dictionary_rules import (
    apply_rules,
    validate_dictionary_constraints,
)


def _apply(key, canonical, core, class_=None, aliases=None, allergens=None,
           confidence=0.9, corpus="msc", forms=None):
    return apply_rules(
        key=key, canonical=canonical, core=core, class_=class_,
        aliases=aliases or [], allergens=allergens or [],
        confidence=confidence, ambiguous=False, corpus=corpus, forms=forms,
    )


def test_r1_quarantine_stoplist() -> None:
    r = _apply("to 6 servings", "to 6 servings", "serving")
    assert r.quarantined and "R1" in r.rules_applied
    r2 = _apply("x", "salt", "salt", confidence=0.3)
    assert r2.quarantined  # confidenza < 0.5


def test_r2_split_compound() -> None:
    r = _apply("carrot, 1⁄2 costa di celery", "carrot", "carrot")
    assert r.split_into is not None and len(r.split_into) == 2
    assert r.split_into[1]["ingredient_core"] == "celery"
    assert "celery" in r.split_into[1]["allergen_tags"]


def test_r3_anti_fusion() -> None:
    r = _apply("CM01099", "mustard", "mustard", class_="condimento")
    assert r.canonical_name_en == "dijon mustard"
    r2 = _apply("CM01156", "milk", "milk")
    assert r2.canonical_name_en == "semi-skimmed milk"
    r3 = _apply("CM01277", "lettuce", "lettuce")
    assert r3.canonical_name_en == "boston lettuce"
    r4 = _apply("CM01797", "white chocolate", "chocolate")
    assert r4.canonical_name_en == "white chocolate substitute"


def test_r4_sulphites_wine() -> None:
    r = _apply("CM00135", "balsamic vinegar", "balsamic vinegar")
    assert "sulphites" in r.allergen_tags
    # distillati esclusi
    r2 = _apply("x", "brandy", "brandy")
    assert "sulphites" not in r2.allergen_tags


def test_r5_celery() -> None:
    r = _apply("x", "celery", "celery")
    assert "celery" in r.allergen_tags
    r2 = _apply("SF00745", "vegetable stock", "vegetable stock")
    assert "celery" in r2.allergen_tags
    # fumet di pesce escluso
    r3 = _apply("x", "fish fumet", "fish fumet")
    assert "celery" not in r3.allergen_tags


def test_r6_gluten() -> None:
    r = _apply("SF00042", "béchamel", "béchamel", allergens=["milk"])
    assert "gluten" in r.allergen_tags
    r2 = _apply("CM00566", "Oyster sauce", "oyster sauce")
    assert "gluten" in r2.allergen_tags
    r3 = _apply("flour", "flour", "flour")
    assert "gluten" in r3.allergen_tags


def test_r7_compositions() -> None:
    r = _apply("RF310169", "dark chocolate ganache", "chocolate ganache")
    assert "milk" in r.allergen_tags
    r2 = _apply("CM01650", "Caesar dressing", "Caesar dressing")
    for a in ("eggs", "fish", "milk"):
        assert a in r2.allergen_tags


def test_r8_soy_coverings() -> None:
    r = _apply("CM06316", "dark chocolate covering", "chocolate",
               forms=["chocolate covering dark barry inaya 65% min."])
    assert "soy" in r.allergen_tags
    r2 = _apply("CM01818", "chocolate", "chocolate",
               forms=["chocolate covering dark 50% min.."])
    assert "soy" in r2.allergen_tags  # riga indicata dal chef


def test_r9_class_canon() -> None:
    r = _apply("CM01027", "Taggiasca olives", "olive", class_="frutta")
    assert r.class_ == "verdura"
    r2 = _apply("CM01377", "vanilla ice cream", "vanilla ice cream", class_="altro")
    assert r2.class_ == "latticino"


def test_constraints_r3_r9() -> None:
    # R3: alias collide col canonical di un altro nodo
    problems = validate_dictionary_constraints([
        {"id": "A", "labels_en": "salt", "aliases": ["table salt"]},
        {"id": "B", "labels_en": "table salt", "aliases": []},
    ])
    assert any("R3" in p for p in problems)
    # R9: stesso canonical con classi diverse
    problems2 = validate_dictionary_constraints([
        {"id": "A", "labels_en": "olive", "class": "frutta"},
        {"id": "B", "labels_en": "olive", "class": "verdura"},
    ])
    assert any("R9" in p for p in problems2)
    # ok
    assert validate_dictionary_constraints([
        {"id": "A", "labels_en": "salt", "class": "condimento"},
    ]) == []
