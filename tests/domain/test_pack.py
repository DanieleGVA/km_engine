"""WP-A1 Domain Pack validation tests."""
from __future__ import annotations

import pytest

from app.domain import (
    DomainPackValidationError,
    load_domain_pack,
    validate_pack,
)


def test_ricette_pack_loads(pack) -> None:
    assert pack.pack.name == "ricette"
    assert pack.pack.language == "it"
    assert pack.pack.canonical_language == "en"
    assert pack.pack.version.startswith("1.0.")  # bump a ogni publish dizionario
    assert pack.template
    assert len(pack.glossary_entries()) >= 15
    assert len(pack.units) >= 10


def test_required_unit_rules_present(pack) -> None:
    """WP-F2: la forma canonica di ogni regola culinaria e' il SINGOLARE.

    Prima ``foglie``/``rametti`` erano ``from_unit`` al plurale e il singolare
    non era riconosciuto da nessuna parte; ora i plurali sono ``from_forms``.
    """
    by_from = pack.unit_rules_by_from()
    required = {
        "g", "kg", "ml", "l", "dl", "°C", "min", "h",
        "cucchiaio", "cucchiaino", "tazza", "pizzico", "spicchio", "foglia",
        "rametto", "bustina", "mazzetto", "fetta", "filo", "costa", "foglio",
    }
    missing = required - set(by_from)
    assert not missing, f"missing unit rules: {sorted(missing)}"
    assert by_from["dl"].to_unit == "ml"
    assert by_from["dl"].factor == 100.0
    assert by_from["cucchiaio"].to_unit == "tablespoon"
    assert by_from["tazza"].to_unit == "cup"
    # i plurali sono forme della stessa regola, non regole a se'
    assert "foglie" in by_from["foglia"].from_forms
    assert "rametti" in by_from["rametto"].from_forms
    assert "leaves" in by_from["foglia"].to_forms


def test_required_glossary_terms_present(pack) -> None:
    entries = {entry.id: entry for entry in pack.glossary_entries()}
    required_ids = {
        "ING-ASPARAGUS", "ING-GRANA", "ING-BUTTER", "ING-SALT",
        "ING-CLAMS", "ING-FREGOLA", "ING-TOMATO-SAUCE", "ING-OLIVE-OIL",
        "ING-FISH-STOCK", "ING-GARLIC", "ING-SHALLOT", "ING-CELERY",
        "ING-PARSLEY", "ING-SWEET-ALMONDS", "ING-BITTER-ALMONDS",
        "ING-SUGAR", "ING-ICING-SUGAR", "ING-EGG-WHITES",
    }
    missing = required_ids - set(entries)
    assert not missing, f"missing glossary entries: {sorted(missing)}"
    assert "grana" in entries["ING-GRANA"].aliases


def test_technique_entries_present(pack) -> None:
    ids = {entry.id for entry in pack.glossaries.tecnica.entries}
    assert {
        "TEC-SOFFRITTO", "TEC-MANTECATURA", "TEC-TOSTATURA",
        "TEC-ROSOLATURA", "TEC-APPASSIMENTO", "TEC-DEPURAZIONE",
    } <= ids


def test_malformed_pack_reports_explicit_errors(tmp_path) -> None:
    pack_dir = tmp_path / "bad-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "name: bad\nlanguage: it\ncanonical_language: en\nversion: 1.0.0\n"
        "glossaries: [tecnica]\nunits_source: units.yaml\n",
        encoding="utf-8",
    )
    errors = validate_pack(pack_dir)
    assert errors
    assert any("template" in error for error in errors)
    assert any("tecnica.yaml" in error for error in errors)
    assert any("units.yaml" in error for error in errors)


def test_malformed_pack_raises_on_load(tmp_path) -> None:
    pack_dir = tmp_path / "bad-pack"
    pack_dir.mkdir()
    (pack_dir / "pack.yaml").write_text(
        "name: bad\nlanguage: it\ncanonical_language: en\nversion: 1.0.0\n"
        "glossaries: [tecnica]\nunits_source: units.yaml\n",
        encoding="utf-8",
    )
    with pytest.raises(DomainPackValidationError) as exc_info:
        load_domain_pack(pack_dir)
    assert exc_info.value.errors


def test_duplicate_glossary_id_is_rejected(tmp_path) -> None:
    pack_dir = tmp_path / "dup-pack"
    (pack_dir / "glossari").mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        "name: dup\nlanguage: it\ncanonical_language: en\nversion: 1.0.0\n"
        "glossaries: [tecnica, ingredienti, stati]\nunits_source: units.yaml\n",
        encoding="utf-8",
    )
    (pack_dir / "template.md").write_text("template\n", encoding="utf-8")
    (pack_dir / "units.yaml").write_text(
        "- rule_id: UNIT-G\n  from_unit: g\n  to_unit: g\n  factor: 1.0\n",
        encoding="utf-8",
    )
    entry = (
        "name: tecnica\nentries:\n"
        "  - id: TEC-X\n    labels_en: x\n    labels_it: x\n    aliases: []\n"
    )
    (pack_dir / "glossari" / "tecnica.yaml").write_text(entry, encoding="utf-8")
    (pack_dir / "glossari" / "ingredienti.yaml").write_text(entry, encoding="utf-8")
    (pack_dir / "glossari" / "stati.yaml").write_text(
        "name: stati\nentries: []\n", encoding="utf-8"
    )
    with pytest.raises(DomainPackValidationError):
        load_domain_pack(pack_dir)


# ---------------------------------------------------------------------------
# WP-F2 — units.yaml sorgente unica
# ---------------------------------------------------------------------------

def test_f2_no_duplicate_unit_tokens(pack) -> None:
    """Nessun token e' forma sorgente di due regole (il parser sarebbe ambiguo)."""
    owner: dict[str, str] = {}
    for rule in pack.units:
        for form in rule.source_forms():
            previous = owner.get(form)
            assert previous is None, (
                f"token {form!r} conteso fra {previous!r} e {rule.rule_id!r}"
            )
            owner[form] = rule.rule_id


def test_f2_target_forms_agree_on_canonical_unit(pack) -> None:
    """Una forma di arrivo non puo' portare a due unita' canoniche diverse."""
    target: dict[str, tuple[str, str]] = {}
    for rule in pack.units:
        for form in rule.target_forms():
            previous = target.get(form)
            assert previous is None or previous[1] == rule.to_unit, (
                f"{form!r} -> {rule.to_unit!r} ({rule.rule_id}) contro "
                f"{previous[1]!r} ({previous[0]})"
            )
            target.setdefault(form, (rule.rule_id, rule.to_unit))


def test_f2_source_form_wins_over_target_form(pack) -> None:
    """``ml`` e' arrivo di UNIT-DL e sorgente di UNIT-ML: vince la sorgente."""
    assert pack.unit_rule_for("ml").rule_id == "UNIT-ML"
    assert pack.unit_rule_for("dl").rule_id == "UNIT-DL"
    assert pack.unit_rule_for("dl").to_unit == "ml"


def test_f2_known_units_come_only_from_units_yaml(pack) -> None:
    """``known_units`` e' esattamente l'unione delle forme dichiarate."""
    expected: set[str] = set()
    for rule in pack.units:
        expected |= rule.forms()
    assert pack.known_units() == expected


def test_f2_duplicate_source_form_is_rejected(tmp_path) -> None:
    """Un pack che contende lo stesso token fra due regole non valida."""
    pack_dir = tmp_path / "dup-units"
    (pack_dir / "glossari").mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(
        "name: dup\nlanguage: it\ncanonical_language: en\nversion: 1.0.0\n"
        "glossaries: [tecnica]\nunits_source: units.yaml\n",
        encoding="utf-8",
    )
    (pack_dir / "template.md").write_text("# t\n", encoding="utf-8")
    (pack_dir / "glossari" / "tecnica.yaml").write_text(
        "name: tecnica\nentries: []\n", encoding="utf-8"
    )
    (pack_dir / "units.yaml").write_text(
        "- rule_id: UNIT-A\n  from_unit: fetta\n  from_forms: [fette]\n"
        "  to_unit: slice\n  factor: 1.0\n  rounding: null\n"
        "- rule_id: UNIT-B\n  from_unit: fette\n  to_unit: piece\n"
        "  factor: 1.0\n  rounding: null\n",
        encoding="utf-8",
    )
    errors = validate_pack(pack_dir)
    assert any("fette" in error and "already claimed" in error for error in errors)


def test_f2_no_hardcoded_unit_tables_left() -> None:
    """Le tabelle duplicate non esistono piu' in nessun modulo."""
    from app.domain import canonical, pack as pack_module, verify

    for module, symbol in (
        (pack_module, "_UNIT_PLURALS"),
        (verify, "DEFAULT_KNOWN_UNITS"),
        (canonical, "_ITALIAN_PLURALS"),
        (canonical, "_ENGLISH_PLURALS"),
        (canonical, "_known_units"),
        (canonical, "_unit_rule_for_token"),
    ):
        assert not hasattr(module, symbol), (
            f"{module.__name__}.{symbol} e' tornato: units.yaml deve restare "
            "la sola sorgente delle unita'"
        )
