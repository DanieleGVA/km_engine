"""WP-F3b — il corpus fixture non contiene piu' dosi inventate.

Il vecchio estrattore pretendeva una cifra all'inizio di ogni riga
ingrediente e, dove il libro non dava una dose, ne inventava una:
``- 1 pizzico sale``, 2.160 righe (19,8% del corpus). Questi test tengono il
corpus onesto: una dose che il libro non da' non deve rientrare.
"""
from __future__ import annotations

import re

import pytest

from app.domain.errors import ParseError
from app.domain.numbers import extract_numbers, mask_numbers, reinject_numbers
from app.domain.verify import parse_source_md
from scripts.build_corpus_fixtures import PINCH_SPICES, transform
from tests.domain.conftest import REPO_ROOT

CORPUS_DIR = REPO_ROOT / "tests" / "fixtures" / "corpus_marchesi_full"

PINCH_LINE_RE = re.compile(r"^- 1 pizzico (?P<item>.+)$")
MAX_PINCH_LINES = 50
EXPECTED_DOCS = 1462


@pytest.fixture(scope="module")
def corpus() -> dict[str, str]:
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CORPUS_DIR.glob("*.md"))
    }


def test_no_injected_pinch(corpus) -> None:
    """Le righe "1 pizzico" superstiti sono poche e sono davvero spezie."""
    survivors: list[tuple[str, str]] = []
    for name, markdown in corpus.items():
        for line in markdown.splitlines():
            match = PINCH_LINE_RE.match(line)
            if match:
                survivors.append((name, match.group("item").strip()))

    assert len(survivors) <= MAX_PINCH_LINES, (
        f"{len(survivors)} righe '1 pizzico' nel corpus: la dose inventata "
        "e' tornata"
    )
    offenders = [
        (name, item)
        for name, item in survivors
        if item.casefold() not in PINCH_SPICES
    ]
    assert not offenders, f"'1 pizzico' su item che non sono spezie: {offenders}"


def test_no_pinch_before_a_quantity(corpus) -> None:
    """Nessun "1 pizzico" davanti a una dose vera ("1 pizzico ½ cipolla")."""
    offenders = [
        (name, line)
        for name, markdown in corpus.items()
        for line in markdown.splitlines()
        if re.match(r"^- 1 pizzico (?:\d|[½⅓⅔¼¾]|il |la |le |un )", line)
    ]
    assert not offenders, offenders[:5]


def test_corpus_parses_after_hygiene(pack, corpus) -> None:
    """Ogni ricetta resta parsabile con la grammatica di F3."""
    known_units = pack.known_units()
    countable_units = pack.countable_units()
    assert len(corpus) == EXPECTED_DOCS
    errors: list[str] = []
    for name, markdown in corpus.items():
        try:
            parse_source_md(
                markdown, known_units=known_units, countable_units=countable_units
            )
        except ParseError as exc:
            errors.append(f"{name}: {exc}")
    assert not errors, errors[:5]


def test_hygiene_script_is_idempotent(corpus) -> None:
    """Rieseguire la bonifica non cambia piu' nulla: e' un punto fisso."""
    for name, markdown in corpus.items():
        rewritten, _ = transform(markdown)
        assert rewritten == markdown, name


def test_to_taste_lines_carry_no_invented_number(pack, corpus) -> None:
    """Una riga "q.b." non porta numeri: la dose e' assente, non uguale a 1."""
    known_units = pack.known_units()
    countable_units = pack.countable_units()
    checked = 0
    for markdown in corpus.values():
        parsed = parse_source_md(
            markdown, known_units=known_units, countable_units=countable_units
        )
        for ingredient in parsed.ingredients:
            if ingredient.to_taste and ingredient.qty is None:
                checked += 1
                assert ingredient.unit is None or ingredient.unit in known_units
    assert checked > 1500, f"attese >1500 righe q.b. dopo la bonifica, {checked}"


def test_p2_fraction_roundtrip() -> None:
    """``½`` e' un numero per P2 ed e' reinserito come glifo originale."""
    body = "## Ingredienti\n- ½ cipolla\n- 1 ½ cucchiaio zucchero\n- 200 g farina 00\n"
    masked, numbers = mask_numbers(body)
    assert numbers == ["½", "1 ½", "200"]
    assert "{N1}" in masked and "½" not in masked
    assert reinject_numbers(masked, numbers) == body.rstrip("\n")

    # il confronto P2 avviene sui valori, non sui glifi
    assert extract_numbers(body) == ["0.5", "1.5", "200"]
    translated = "## Ingredients\n- 0.5 onion\n- 1.5 tablespoon sugar\n- 200 g flour 00\n"
    assert extract_numbers(translated) == extract_numbers(body)


def test_p2_holds_on_the_whole_corpus(pack, corpus) -> None:
    """Le frazioni del corpus non rompono l'estrazione numeri su nessuna ricetta."""
    for name, markdown in corpus.items():
        numbers = extract_numbers(markdown)
        masked, masked_numbers = mask_numbers(markdown)
        assert len(masked_numbers) >= len(numbers), name
        assert reinject_numbers(masked, masked_numbers) == markdown.rstrip("\n"), name
