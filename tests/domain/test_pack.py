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
    by_from = pack.unit_rules_by_from()
    required = {
        "g", "kg", "ml", "l", "dl", "°C", "min", "h",
        "cucchiaio", "tazza", "pizzico", "spicchio", "foglie",
        "rametti", "bustina", "mazzetto",
    }
    missing = required - set(by_from)
    assert not missing, f"missing unit rules: {sorted(missing)}"
    assert by_from["dl"].to_unit == "ml"
    assert by_from["dl"].factor == 100.0
    assert by_from["cucchiaio"].to_unit == "tablespoon"
    assert by_from["tazza"].to_unit == "cup"


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
