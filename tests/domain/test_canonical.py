"""WP-A5 — canonicalizzazione deterministica + canon-log + coda proposte.

Copre T2 (conversioni unità), T8-bis (serializzazione Appendix A), T9
(canon-log completo, invariante bidirezionale) e T10 (termini irrisolti).
Prefisso test: ``ia5_``. Pulizia Postgres: ``ia_`` (vedi conftest).
"""
from __future__ import annotations

import re

from app.domain import (
    FakeLLMClient,
    build_translation_input,
    canonicalize,
    format_quantity,
    generate_canon_log,
    list_glossary_proposals,
    mask_numbers,
    normalize_terms,
    parse_source_md,
    translate_document,
    verify_canon_log,
    write_canon_log,
)

from .conftest import read_corpus, real_recipe_names

# (from_unit, qty, expected_qty, expected_unit, expected_rule_id_or_None)
UNIT_CASES = [
    ("g", "100", "100", "g", None),
    ("kg", "1.5", "1.5", "kg", None),
    ("ml", "250", "250", "ml", None),
    ("l", "0.1", "0.1", "l", None),
    ("dl", "1", "100", "ml", "UNIT-DL"),
    ("°C", "200", "200", "°C", None),
    ("min", "5", "5", "min", None),
    ("h", "2", "2", "h", None),
    ("cucchiaio", "2", "2", "tablespoon", "UNIT-TBSP"),
    ("tazza", "1", "1", "cup", "UNIT-CUP"),
    ("pizzico", "1", "1", "pinch", "UNIT-PINCH"),
    ("spicchio", "1", "1", "clove", "UNIT-CLOVE"),
    ("foglie", "5", "5", "leaf", "UNIT-LEAF"),
    ("rametti", "4", "4", "sprig", "UNIT-SPRIG"),
    ("bustina", "1", "1", "sachet", "UNIT-SACHET"),
    ("mazzetto", "1", "1", "bunch", "UNIT-BUNCH"),
]


def _translated_md(qty: str, unit: str | None, item: str, doc_id: str = "ia5-T") -> str:
    ingredient = f"- {qty} {unit} {item}" if unit else f"- {qty} {item}"
    return (
        "---\n"
        f"title: Test\nid: {doc_id}\nlang: en\nsource_lang: it\n"
        "servings: 1\ntime_min: 1\ndifficulty: easy\n---\n"
        "## Ingredients\n"
        f"{ingredient}\n"
        "## Method\n"
        "1. Cook.\n"
    )


def _ingredient_lines(md: str) -> list[str]:
    return [line for line in md.splitlines() if line.startswith("- ")]


_PLACEHOLDER_RE = re.compile(r"\{N\d+\}")


def _translate_masked(pack, masked_input: str) -> str:
    """Deterministic glossary-based translation for FakeLLMClient fixtures.

    ``normalize_terms`` lowercases its input, which would turn ``{N1}`` into
    ``{n1}`` and break re-injection. Placeholders are protected with a sentinel
    and restored after translation.
    """
    placeholders = _PLACEHOLDER_RE.findall(masked_input)
    protected = _PLACEHOLDER_RE.sub("\x00", masked_input)
    lines: list[str] = []
    for line in protected.splitlines():
        stripped = line.strip()
        if stripped == "## Ingredienti":
            lines.append("## Ingredients")
        elif stripped == "## Procedimento":
            lines.append("## Method")
        else:
            lines.append(normalize_terms(line, pack.it_to_en_terms()))
    translated = "\n".join(lines)
    for placeholder in placeholders:
        translated = translated.replace("\x00", placeholder, 1)
    return translated


def _build_fake_llm(pack, corpus: dict[str, str]) -> FakeLLMClient:
    translations: dict[str, str] = {}
    for source_md in corpus.values():
        parsed = parse_source_md(source_md, known_units=pack.known_units())
        masked_input, _ = mask_numbers(build_translation_input(parsed))
        translations[masked_input] = _translate_masked(pack, masked_input)
    return FakeLLMClient(translations)


# ---------------------------------------------------------------------------
# T2 — conversioni unità
# ---------------------------------------------------------------------------

def test_ia5_t2_all_16_unit_rules(pack) -> None:
    for from_unit, qty, expected_qty, expected_unit, rule_id in UNIT_CASES:
        doc = canonicalize(pack, _translated_md(qty, from_unit, "test item"))
        lines = _ingredient_lines(doc.canonical_md)
        assert lines == [f"- {expected_qty} {expected_unit} test item"], (
            from_unit, doc.canonical_md
        )

        unit_entries = [
            entry
            for entry in doc.log_entries
            if entry.field.endswith(".qty") or entry.field.endswith(".unit")
        ]
        if rule_id is None:
            assert unit_entries == [], (from_unit, unit_entries)
        else:
            assert unit_entries, from_unit
            assert all(entry.rule_id == rule_id for entry in unit_entries), (
                from_unit, unit_entries
            )


def test_ia5_t2_italian_plural_units(pack) -> None:
    cases = [
        ("cucchiai", "2", "tablespoon", "UNIT-TBSP"),
        ("tazze", "1", "cup", "UNIT-CUP"),
        ("spicchi", "2", "clove", "UNIT-CLOVE"),
        ("pizzichi", "1", "pinch", "UNIT-PINCH"),
        ("bustine", "1", "sachet", "UNIT-SACHET"),
        ("mazzetti", "1", "bunch", "UNIT-BUNCH"),
    ]
    for unit, qty, expected_unit, rule_id in cases:
        doc = canonicalize(pack, _translated_md(qty, unit, "test item"))
        assert _ingredient_lines(doc.canonical_md) == [
            f"- {qty} {expected_unit} test item"
        ]
        unit_entries = [e for e in doc.log_entries if e.field.endswith(".unit")]
        assert unit_entries and unit_entries[0].rule_id == rule_id


def test_ia5_t2_english_plural_units(pack) -> None:
    cases = [
        ("tablespoons", "2", "tablespoon", "UNIT-TBSP"),
        ("cups", "1", "cup", "UNIT-CUP"),
        ("cloves", "2", "clove", "UNIT-CLOVE"),
        ("leaves", "5", "leaf", "UNIT-LEAF"),
        ("sprigs", "4", "sprig", "UNIT-SPRIG"),
        ("sachets", "1", "sachet", "UNIT-SACHET"),
        ("bunches", "1", "bunch", "UNIT-BUNCH"),
    ]
    for unit, qty, expected_unit, rule_id in cases:
        doc = canonicalize(pack, _translated_md(qty, unit, "test item"))
        assert _ingredient_lines(doc.canonical_md) == [
            f"- {qty} {expected_unit} test item"
        ]
        unit_entries = [e for e in doc.log_entries if e.field.endswith(".unit")]
        assert unit_entries and unit_entries[0].rule_id == rule_id


def test_ia5_t2_decimal_no_float_error(pack) -> None:
    doc = canonicalize(pack, _translated_md("0.1", "dl", "test item"))
    assert _ingredient_lines(doc.canonical_md) == ["- 10 ml test item"]
    qty_entries = [e for e in doc.log_entries if e.field.endswith(".qty")]
    assert qty_entries and qty_entries[0].after_text == "10"
    assert qty_entries[0].rule_id == "UNIT-DL"


def test_ia5_t2_rounding_half_up(pack) -> None:
    # 0.125 dl = 12.5 ml -> 13 ml (rounding=0, half-up dichiarato).
    doc = canonicalize(pack, _translated_md("0.125", "dl", "test item"))
    assert _ingredient_lines(doc.canonical_md) == ["- 13 ml test item"]


def test_ia5_t2_identity_units_unchanged(pack) -> None:
    for unit, qty in [("kg", "1.5"), ("°C", "200"), ("min", "5"), ("l", "0.1")]:
        doc = canonicalize(pack, _translated_md(qty, unit, "test item"))
        assert _ingredient_lines(doc.canonical_md) == [f"- {qty} {unit} test item"]
        assert not [
            e for e in doc.log_entries if e.field.endswith(".qty") or e.field.endswith(".unit")
        ]


# ---------------------------------------------------------------------------
# T8-bis — serializzazione Appendix A
# ---------------------------------------------------------------------------

def test_ia5_t8bis_format_quantity() -> None:
    assert format_quantity(3) == "3"
    assert format_quantity(3.0) == "3"
    assert format_quantity(0.5) == "0.5"
    assert format_quantity(1.25) == "1.25"
    assert format_quantity(0.125) == "0.125"
    assert format_quantity(0.1234) == "0.123"


def test_ia5_t8bis_qty_without_unit(pack) -> None:
    doc = canonicalize(pack, _translated_md("3", None, "uova"))
    assert _ingredient_lines(doc.canonical_md) == ["- 3 egg"]


def test_ia5_t8bis_single_trailing_newline_and_frontmatter_order(pack) -> None:
    doc = canonicalize(pack, _translated_md("1", "dl", "test item"))
    assert doc.canonical_md.endswith("\n")
    assert not doc.canonical_md.endswith("\n\n")
    lines = doc.canonical_md.splitlines()
    fm_start = lines.index("---")
    fm_end = lines.index("---", fm_start + 1)
    fm_keys = [line.split(":")[0] for line in lines[fm_start + 1:fm_end]]
    assert fm_keys == [
        "title", "id", "lang", "source_lang", "servings", "time_min",
        "difficulty", "verification_level", "canonical_version",
    ]


# ---------------------------------------------------------------------------
# T9 — canon-log completo (invariante bidirezionale)
# ---------------------------------------------------------------------------

async def test_ia5_t9_canon_log_real_recipes(pack) -> None:
    corpus = read_corpus()
    names = real_recipe_names()
    llm = _build_fake_llm(pack, {name: corpus[name] for name in names})
    for name in names:
        translated = await translate_document(pack, corpus[name], llm)
        doc = canonicalize(pack, translated.translated_md)
        assert verify_canon_log(
            pack, translated.translated_md, doc.canonical_md, doc.log_entries
        ), name


async def test_ia5_t9_canon_log_synthetic_recipes(pack) -> None:
    corpus = read_corpus()
    synthetic = {
        name: md for name, md in corpus.items() if name not in real_recipe_names()
    }
    llm = _build_fake_llm(pack, synthetic)
    for name, source_md in synthetic.items():
        translated = await translate_document(pack, source_md, llm)
        doc = canonicalize(pack, translated.translated_md)
        assert verify_canon_log(
            pack, translated.translated_md, doc.canonical_md, doc.log_entries
        ), name


def test_ia5_t9_generate_canon_log_matches_canonicalize(pack) -> None:
    md = _translated_md("1", "dl", "test item", doc_id="ia5-G")
    doc = canonicalize(pack, md)
    regenerated = generate_canon_log(pack, md, doc.canonical_md)
    assert regenerated == doc.log_entries


# ---------------------------------------------------------------------------
# T10 — termini irrisolti in coda proposte, mai riscritti
# ---------------------------------------------------------------------------

def test_ia5_t10_unresolved_terms_queued_and_not_rewritten(pack, pg_conn) -> None:
    md = (
        "---\n"
        "title: Amaretti\nid: ia5-RIC-103\nlang: en\nsource_lang: it\n"
        "servings: 4\ntime_min: 55\ndifficulty: medium\n---\n"
        "## Ingredients\n"
        "- 120 g mandorle dolci sbucciate\n"
        "- 80 g mandorle amare sbucciate\n"
        "- 160 g sugar\n"
        "## Method\n"
        "1. Toast the almonds.\n"
    )
    doc = canonicalize(pack, md, conn=pg_conn)

    # Il termine NON viene riscritto e qty/unità restano.
    assert _ingredient_lines(doc.canonical_md) == [
        "- 120 g mandorle dolci sbucciate",
        "- 80 g mandorle amare sbucciate",
        "- 160 g sugar",
    ]
    # Nessun id glossario inventato per i termini irrisolti.
    assert "ING-SWEET-ALMONDS" not in doc.canonical_md
    assert "ING-BITTER-ALMONDS" not in doc.canonical_md
    assert doc.unresolved_terms == [
        "mandorle dolci sbucciate",
        "mandorle amare sbucciate",
    ]

    proposals = list_glossary_proposals(pg_conn, status="pending")
    terms = {proposal["term"] for proposal in proposals}
    assert "mandorle dolci sbucciate" in terms
    assert "mandorle amare sbucciate" in terms
    for proposal in proposals:
        if proposal["term"] in terms:
            assert proposal["status"] == "pending"
            assert proposal["context"] == "ia5-RIC-103"


def test_ia5_t10_no_proposals_without_conn(pack) -> None:
    md = _translated_md("120", "g", "mandorle dolci sbucciate", doc_id="ia5-NC")
    doc = canonicalize(pack, md, conn=None)
    assert doc.unresolved_terms == ["mandorle dolci sbucciate"]
    assert _ingredient_lines(doc.canonical_md) == [
        "- 120 g mandorle dolci sbucciate"
    ]


# ---------------------------------------------------------------------------
# Persistenza canon_log
# ---------------------------------------------------------------------------

def test_ia5_write_canon_log_persists(pack, pg_conn) -> None:
    md = _translated_md("1", "dl", "test item", doc_id="ia5-W")
    doc = canonicalize(pack, md)
    written = write_canon_log(pg_conn, doc.log_entries)
    assert written == len(doc.log_entries)

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT document_id, field, before_text, after_text, rule_id "
            "FROM canon_log WHERE document_id = %s ORDER BY id",
            ("ia5-W",),
        )
        rows = cur.fetchall()
    assert len(rows) == written
    unit_rows = [row for row in rows if row[1] == "ingredients[0].unit"]
    assert unit_rows and unit_rows[0][4] == "UNIT-DL"
