"""WP-F2 — le unita' culinarie vengono davvero consumate come unita'.

Il difetto D3 era invisibile ai test unitari: ``units.yaml`` dichiarava
``rametti`` (plurale) e il corpus scriveva ``1 rametto di rosmarino``, cosi'
il token finiva dentro l'item e il termine non si risolveva piu'. Questo test
guarda il corpus intero, non un caso scelto a mano.
"""
from __future__ import annotations

from collections import Counter

import pytest

from app.domain.normalize import normalize_key
from app.domain.verify import parse_source_md
from tests.domain.conftest import REPO_ROOT

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"

# Token che, se aprono un item, sono quasi certamente un'unita' non consumata.
SUSPECT_UNIT_TOKENS = frozenset({
    "cucchiaio", "cucchiai", "cucchiaino", "cucchiaini",
    "tazza", "tazze", "tazzina", "tazzine", "bicchiere", "bicchieri",
    "pizzico", "pizzichi", "presa", "prese", "manciata", "manciate",
    "spicchio", "spicchi", "foglia", "foglie", "rametto", "rametti",
    "ciuffo", "ciuffi", "mazzetto", "mazzetti", "mazzo", "mazzi",
    "bustina", "bustine", "fetta", "fette", "fettina", "fettine",
    "filetto", "filetti", "filo", "fili", "costa", "coste",
    "costola", "costole", "gambo", "gambi", "grani", "bacca", "bacche",
    "foglio", "fogli", "pezzo", "pezzi",
})

# Token deliberatamente NON promossi a unita' (decisione presa sul corpus,
# documentata in units.yaml): qui sono ingredienti, non dosi.
NOT_UNITS_BY_DECISION = frozenset({
    "noce", "noci", "chiodo", "chiodi", "goccia", "gocce", "grano",
})

MAX_ALLOWED_OCCURRENCES = 5


@pytest.fixture(scope="module")
def leading_tokens(pack) -> Counter:
    """Primo token dell'item, per le righe in cui il parser non ha visto unita'."""
    counter: Counter[str] = Counter()
    known_units = pack.known_units()
    countable_units = pack.countable_units()
    for path in sorted(CORPUS_DIR.glob("*.md")):
        doc = parse_source_md(
            path.read_text(encoding="utf-8"),
            known_units=known_units,
            countable_units=countable_units,
        )
        for ingredient in doc.ingredients:
            if ingredient.unit is not None:
                continue
            tokens = normalize_key(ingredient.item).split()
            if tokens:
                counter[tokens[0]] += 1
    return counter


def test_f2_suspect_unit_tokens_are_consumed(leading_tokens) -> None:
    """Nessun token-unita' apre ancora un item con frequenza significativa."""
    leftovers = {
        token: count
        for token, count in leading_tokens.items()
        if token in SUSPECT_UNIT_TOKENS and count >= MAX_ALLOWED_OCCURRENCES
    }
    assert not leftovers, f"unita' non consumate dal parser: {leftovers}"


def test_f2_excluded_tokens_stay_in_the_item(pack) -> None:
    """I token esclusi per decisione non sono unita': l'ingrediente resta intero."""
    known_units = pack.known_units()
    for token in NOT_UNITS_BY_DECISION:
        assert token not in known_units, (
            f"{token!r} e' stato promosso a unita': su questo corpus e' un "
            "ingrediente (vedi la nota in units.yaml)"
        )


def test_f2_singular_and_plural_reach_the_same_rule(pack) -> None:
    """Le due forme dello stesso concetto portano alla stessa regola."""
    for singular, plural in (
        ("rametto", "rametti"),
        ("foglia", "foglie"),
        ("fetta", "fette"),
        ("cucchiaio", "cucchiai"),
        ("spicchio", "spicchi"),
        ("costa", "coste"),
    ):
        rule_singular = pack.unit_rule_for(singular)
        rule_plural = pack.unit_rule_for(plural)
        assert rule_singular is not None, singular
        assert rule_singular is rule_plural, (singular, plural)


def test_f2_english_plurals_reach_the_same_rule(pack) -> None:
    """Anche le forme inglesi del documento tradotto: una sola sorgente."""
    for singular, plural in (
        ("tablespoon", "tablespoons"),
        ("clove", "cloves"),
        ("leaf", "leaves"),
        ("sprig", "sprigs"),
        ("slice", "slices"),
    ):
        rule = pack.unit_rule_for(singular)
        assert rule is not None, singular
        assert rule is pack.unit_rule_for(plural), (singular, plural)
        assert rule.to_unit == singular
