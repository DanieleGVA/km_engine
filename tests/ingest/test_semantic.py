"""Tests for the deterministic semantic stub and the LLM skeleton."""

from __future__ import annotations

import pytest

from app.ingest.semantic import LLMSemanticService, StubSemanticService


def test_stub_english_text_is_native() -> None:
    service = StubSemanticService()
    facts = service.analyze_text(
        "# Hello\n\nThe system is under development.",
        source_uri="/tmp/hello.md",
    )
    assert len(facts) == 2
    assert all(f.language == "en" for f in facts)
    assert all(f.translation_state == "native" for f in facts)
    assert all(f.source_language is None for f in facts)


def test_stub_french_text_simulates_translation() -> None:
    service = StubSemanticService()
    text = "# Gestion\n\nLe système de gestion des connaissances est en cours."
    facts = service.analyze_text(text, source_uri="/tmp/gestion.md")
    assert len(facts) == 2
    assert all(f.language == "en" for f in facts)
    assert all(f.translation_state == "pending" for f in facts)
    assert all(f.source_language == "fr" for f in facts)
    summary = next(f for f in facts if f.property == "summary")
    assert summary.value.startswith("[EN] ")


def test_stub_translate_flow_is_invocable() -> None:
    service = StubSemanticService()
    assert service.translate_to_english("Hello world") == "Hello world"
    translated = service.translate_to_english(
        "Le système est en cours de développement."
    )
    assert translated.startswith("[EN] ")


def test_stub_image_is_deterministic() -> None:
    service = StubSemanticService()
    facts = service.analyze_image(b"fake-image-bytes", source_uri="/tmp/logo.png")
    assert len(facts) == 1
    assert facts[0].property == "description"
    assert "logo.png" in facts[0].value
    assert facts[0].language == "en"


def test_llm_skeleton_does_not_call_network() -> None:
    service = LLMSemanticService(api_key="x", endpoint="http://localhost:9", model="m")
    with pytest.raises(NotImplementedError):
        service.translate_to_english("Le système est en cours.")
