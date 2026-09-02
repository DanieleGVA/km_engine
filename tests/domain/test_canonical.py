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
from app.domain.canonical import _build_term_map
from app.domain.normalize import normalize_key

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
        "- 120 g funghi porcini\n"
        "- 80 g brodo di carne\n"
        "- 160 g sugar\n"
        "## Method\n"
        "1. Toast the almonds.\n"
    )
    doc = canonicalize(pack, md, conn=pg_conn)

    # Il termine NON viene riscritto e qty/unità restano.
    assert _ingredient_lines(doc.canonical_md) == [
        "- 120 g funghi porcini",
        "- 80 g brodo di carne",
        "- 160 g sugar",
    ]
    assert doc.unresolved_terms == ["funghi porcini", "brodo di carne"]

    proposals = list_glossary_proposals(pg_conn, status="pending")
    terms = {proposal["term"] for proposal in proposals}
    assert "funghi porcini" in terms
    # WP-F4: la proposta porta i candidati piu' vicini, cosi' chi lavora la
    # coda vede se manca un alias o serve una voce nuova.
    proposal = next(p for p in proposals if p["term"] == "brodo di carne")
    assert proposal["candidates"], proposal
    assert {"key", "score"} <= set(proposal["candidates"][0])
    assert "brodo di carne" in terms
    for proposal in proposals:
        if proposal["term"] in terms:
            assert proposal["status"] == "pending"
            assert proposal["context"] == "ia5-RIC-103"


def test_ia5_t10_no_proposals_without_conn(pack) -> None:
    md = _translated_md("120", "g", "funghi porcini", doc_id="ia5-NC")
    doc = canonicalize(pack, md, conn=None)
    assert doc.unresolved_terms == ["funghi porcini"]
    assert _ingredient_lines(doc.canonical_md) == ["- 120 g funghi porcini"]


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


# ---------------------------------------------------------------------------
# WP-F1 — simmetria della chiave di lookup (D2)
# ---------------------------------------------------------------------------

def test_f1_glossary_symmetry(pack) -> None:
    """Ogni termine del glossario e' raggiungibile dalla propria chiave.

    Se un solo termine non si trova nella mappa costruita da se stesso, il
    lookup e la costruzione della mappa usano due normalizzazioni diverse:
    e' esattamente il difetto D2, in forma testabile.
    """
    term_map = _build_term_map(pack)
    for entry in pack.glossary_entries():
        for term in (entry.labels_en, entry.labels_it, *entry.aliases):
            key = normalize_key(term)
            if not key:
                continue
            assert key in term_map, f"{entry.id}: {term!r} -> {key!r}"


def test_f1_regression_d2(pack) -> None:
    """Le tre forme in cui la traduzione consegna l'olio risolvono tutte."""
    for item in (
        "di extra virgin olive oil",
        "extra virgin olive oil",
        "di olio extravergine di oliva",
        "olio extravergine d’oliva",
    ):
        doc = canonicalize(pack, _translated_md("1", "dl", item, doc_id="ia5-F1"))
        assert doc.unresolved_terms == [], item
        assert _ingredient_lines(doc.canonical_md) == [
            "- 100 ml extra virgin olive oil"
        ], item


def test_f1_compound_conjunction_resolves(pack) -> None:
    """``sale e pepe`` e' una voce di glossario: la ``e`` non va rimossa."""
    doc = canonicalize(pack, _translated_md("1", None, "sale e pepe", doc_id="ia5-F1b"))
    assert doc.unresolved_terms == []


def test_f1_elision_resolves(pack) -> None:
    """``d'aglio`` (292 righe nel corpus) risolve come ``aglio``."""
    doc = canonicalize(pack, _translated_md("2", "clove", "d’aglio", doc_id="ia5-F1c"))
    assert doc.unresolved_terms == []
    assert _ingredient_lines(doc.canonical_md) == ["- 2 clove garlic"]


def test_f1_unresolved_still_untouched(pack) -> None:
    """T10 non cambia: un termine non risolto non viene mai riscritto."""
    doc = canonicalize(
        pack, _translated_md("120", "g", "funghi porcini", doc_id="ia5-F1d")
    )
    assert doc.unresolved_terms == ["funghi porcini"]
    assert _ingredient_lines(doc.canonical_md) == ["- 120 g funghi porcini"]


# ---------------------------------------------------------------------------
# WP-F4 — stati e preparazione nel markdown canonico (opzione A)
# ---------------------------------------------------------------------------

def test_f4_states_are_kept_in_the_markdown(pack) -> None:
    """"mandorle dolci sbucciate" -> "sweet almonds [peeled]": niente si perde."""
    doc = canonicalize(
        pack, _translated_md("120", "g", "mandorle dolci sbucciate", doc_id="ia5-F4a")
    )
    assert _ingredient_lines(doc.canonical_md) == ["- 120 g sweet almonds [peeled]"]
    assert doc.unresolved_terms == []
    assert doc.parsed.ingredients[0].state == ("peeled",)


def test_f4_unresolved_head_keeps_the_item_whole(pack) -> None:
    """Testa non risolta: l'item resta intero, lo stato non viene duplicato.

    L'informazione "sotto sale" c'e' gia', dentro l'item che T10 vieta di
    riscrivere; aggiungerla anche in coda come ``[salted]`` la ripeterebbe.
    Lo stato staccato resta comunque nella ``Resolution``, che alimenta la
    coda proposte (WP-F5).
    """
    doc = canonicalize(
        pack, _translated_md("50", "g", "capperi sotto sale", doc_id="ia5-F4b")
    )
    assert _ingredient_lines(doc.canonical_md) == ["- 50 g capperi sotto sale"]
    assert doc.unresolved_terms == ["capperi sotto sale"]


def test_f4_prep_and_inner_quantity(pack) -> None:
    """La dose scritta dentro l'item diventa la dose della riga."""
    md = (
        "---\n"
        "title: Test\nid: ia5-F4c\nlang: en\nsource_lang: it\n"
        "servings: 1\ntime_min: 1\ndifficulty: easy\n---\n"
        "## Ingredients\n"
        "- to taste il succo di 1 limone\n"
        "## Method\n"
        "1. Cook.\n"
    )
    doc = canonicalize(pack, md)
    assert _ingredient_lines(doc.canonical_md) == ["- 1 lemon (juice)"]
    ingredient = doc.parsed.ingredients[0]
    assert ingredient.qty == "1"
    assert ingredient.prep == "juice"


def test_f4_canon_log_explains_state_and_prep(pack) -> None:
    """T9 resta bidirezionale con stati e preparazione sulla riga."""
    md = (
        "---\n"
        "title: Test\nid: ia5-F4d\nlang: en\nsource_lang: it\n"
        "servings: 1\ntime_min: 1\ndifficulty: easy\n---\n"
        "## Ingredients\n"
        "- 120 g mandorle dolci sbucciate\n"
        "- to taste il succo di 1 limone\n"
        "## Method\n"
        "1. Cook.\n"
    )
    doc = canonicalize(pack, md)
    fields = {entry.field for entry in doc.log_entries}
    assert "ingredients[0].state" in fields
    assert "ingredients[1].prep" in fields
    state_entry = next(
        entry for entry in doc.log_entries if entry.field == "ingredients[0].state"
    )
    assert state_entry.before_text == ""
    assert state_entry.after_text == "peeled"
    assert state_entry.rule_id == "STA-SBUCCIATO"
    assert verify_canon_log(pack, md, doc.canonical_md, doc.log_entries)


def test_f4_by_rule_counts_every_line(pack) -> None:
    """``by_rule`` copre tutte le righe: la misura non perde nessun caso."""
    md = (
        "---\n"
        "title: Test\nid: ia5-F4e\nlang: en\nsource_lang: it\n"
        "servings: 1\ntime_min: 1\ndifficulty: easy\n---\n"
        "## Ingredients\n"
        "- 1 clove garlic\n"
        "- 1 dl olio evo\n"
        "- 120 g mandorle dolci sbucciate\n"
        "- 80 g funghi porcini\n"
        "## Method\n"
        "1. Cook.\n"
    )
    doc = canonicalize(pack, md)
    assert doc.by_rule == {
        "GLOSS-EXACT": 1,
        "GLOSS-ALIAS": 1,
        "GLOSS-HEAD": 1,
        "GLOSS-UNRESOLVED": 1,
    }
    assert sum(doc.by_rule.values()) == len(doc.parsed.ingredients)
    assert doc.unresolved_candidates["funghi porcini"]
