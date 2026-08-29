"""Tests for FR9 language normalization."""

from __future__ import annotations

from app.ingest.language import normalize_language


def test_english_is_canonical_native() -> None:
    info = normalize_language("The system is under active development.")
    assert info.detected == "en"
    assert info.canonical == "en"
    assert info.needs_translation is False
    assert info.translation_state == "native"
    assert info.source_language is None


def test_french_is_marked_for_translation() -> None:
    text = "Le système de gestion des connaissances est en cours de développement."
    info = normalize_language(text)
    assert info.detected == "fr"
    assert info.canonical == "en"
    assert info.needs_translation is True
    assert info.translation_state == "pending"
    assert info.source_language == "fr"


def test_german_is_marked_for_translation() -> None:
    text = "Das System für die Verwaltung von Wissen wird derzeit entwickelt."
    info = normalize_language(text)
    assert info.detected == "de"
    assert info.needs_translation is True
    assert info.translation_state == "pending"


def test_empty_text_defaults_to_english() -> None:
    info = normalize_language("")
    assert info.detected == "en"
    assert info.needs_translation is False
