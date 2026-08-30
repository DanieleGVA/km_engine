"""Integration/unit: Document localisation FR9.3 (WP-B4, gate GB4)."""
from __future__ import annotations

from app.rag.rag import localize_document


def test_ib_localize_native_source_language() -> None:
    doc = {"source_language": "it", "translation_state": "translated"}
    assert "untranslated" not in localize_document(doc, "it")


def test_ib_localize_english_canonical_is_ok() -> None:
    doc = {"source_language": "it", "translation_state": "translated"}
    assert "untranslated" not in localize_document(doc, "en")


def test_ib_localize_other_language_gets_flag() -> None:
    doc = {"source_language": "it", "translation_state": "translated"}
    result = localize_document(doc, "fr")
    assert result["untranslated"] is True


def test_ib_localize_no_lang_no_flag() -> None:
    doc = {"source_language": "it", "translation_state": "translated"}
    assert "untranslated" not in localize_document(doc, None)


def test_ib_localize_does_not_mutate_input() -> None:
    doc = {"source_language": "it", "translation_state": "translated"}
    localize_document(doc, "fr")
    assert "untranslated" not in doc
