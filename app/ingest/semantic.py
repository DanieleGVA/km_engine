"""Semantic pass for documents and images (FR1.2/FR1.3, FR9).

``SemanticService`` is the interface. ``StubSemanticService`` is deterministic
and never calls an external LLM. ``LLMSemanticService`` is a documented adapter
skeleton: configure ``KM_LLM_API_KEY``/``KM_LLM_ENDPOINT``/``KM_LLM_MODEL`` and
implement the HTTP call in a later iteration. Tests use the stub only.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path

from app.ingest.language import normalize_language
from app.ingest.models import CandidateFact

_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")


class SemanticService(ABC):
    """Analyze text or image content into candidate facts."""

    @abstractmethod
    def analyze_text(
        self,
        text: str,
        *,
        source_uri: str,
        source_location: str | None = None,
    ) -> list[CandidateFact]:
        """Return candidate facts for a text document."""

    @abstractmethod
    def analyze_image(
        self,
        image_bytes: bytes,
        *,
        source_uri: str,
        source_location: str | None = None,
    ) -> list[CandidateFact]:
        """Return candidate facts for an image."""

    @abstractmethod
    def translate_to_english(self, text: str) -> str:
        """Return the canonical English representation of ``text`` (FR9.2)."""


def _first_sentence(text: str) -> str:
    parts = [p.strip() for p in _SENTENCE_RE.split(text.strip()) if p.strip()]
    return parts[0] if parts else text.strip()


def _title(text: str, source_uri: str) -> str:
    """First markdown heading, else the source basename stem."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip() or Path(source_uri).stem
    return Path(source_uri).stem


class StubSemanticService(SemanticService):
    """Deterministic semantic service for tests and local development.

    It simulates FR9 translation by returning English facts with
    ``translation_state='pending'`` and ``source_language`` set when the source
    is not English. No network or LLM call is made.
    """

    def translate_to_english(self, text: str) -> str:
        info = normalize_language(text)
        if not info.needs_translation:
            return text
        return f"[EN] {_first_sentence(text)}"

    def analyze_text(
        self,
        text: str,
        *,
        source_uri: str,
        source_location: str | None = None,
    ) -> list[CandidateFact]:
        info = normalize_language(text)
        title = _title(text, source_uri)
        summary = self.translate_to_english(text)
        return [
            CandidateFact(
                entity_label=title,
                property="title",
                value=title,
                confidence="INFERRED",
                language="en",
                translation_state=info.translation_state,
                source_language=info.source_language,
            ),
            CandidateFact(
                entity_label=title,
                property="summary",
                value=summary,
                confidence="INFERRED",
                language="en",
                translation_state=info.translation_state,
                source_language=info.source_language,
            ),
        ]

    def analyze_image(
        self,
        image_bytes: bytes,
        *,
        source_uri: str,
        source_location: str | None = None,
    ) -> list[CandidateFact]:
        name = Path(source_uri).stem
        return [
            CandidateFact(
                entity_label=name,
                property="description",
                value=f"[EN] image {Path(source_uri).name} ({len(image_bytes)} bytes)",
                confidence="INFERRED",
                language="en",
                translation_state="native",
                source_language=None,
            )
        ]


class LLMSemanticService(SemanticService):
    """Documented LLM adapter skeleton.

    Configuration (env vars, read by :class:`app.ingest.config.IngestSettings`):
    - ``KM_LLM_API_KEY``: provider API key.
    - ``KM_LLM_ENDPOINT``: OpenAI-compatible chat completions endpoint.
    - ``KM_LLM_MODEL``: model name.

    The HTTP call is intentionally not implemented in the MVP and is never
    invoked by tests. Implement ``_complete`` with the provider SDK/HTTP client
    of choice, then call it from the three methods below.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        endpoint: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model or "default"

    def _complete(self, prompt: str) -> str:
        raise NotImplementedError(
            "LLMSemanticService is a skeleton. Configure KM_LLM_API_KEY, "
            "KM_LLM_ENDPOINT and KM_LLM_MODEL, then implement _complete()."
        )

    def translate_to_english(self, text: str) -> str:
        info = normalize_language(text)
        if not info.needs_translation:
            return text
        return self._complete(
            "Translate the following text to English, preserving meaning, "
            "terminology and relations:\n\n" + text
        )

    def analyze_text(
        self,
        text: str,
        *,
        source_uri: str,
        source_location: str | None = None,
    ) -> list[CandidateFact]:
        info = normalize_language(text)
        title = _title(text, source_uri)
        summary = self.translate_to_english(text)
        return [
            CandidateFact(
                entity_label=title,
                property="title",
                value=title,
                confidence="INFERRED",
                language="en",
                translation_state=info.translation_state,
                source_language=info.source_language,
            ),
            CandidateFact(
                entity_label=title,
                property="summary",
                value=summary,
                confidence="INFERRED",
                language="en",
                translation_state=info.translation_state,
                source_language=info.source_language,
            ),
        ]

    def analyze_image(
        self,
        image_bytes: bytes,
        *,
        source_uri: str,
        source_location: str | None = None,
    ) -> list[CandidateFact]:
        name = Path(source_uri).stem
        description = self._complete(
            f"Describe the image {Path(source_uri).name} ({len(image_bytes)} bytes)."
        )
        return [
            CandidateFact(
                entity_label=name,
                property="description",
                value=description,
                confidence="INFERRED",
                language="en",
                translation_state="native",
                source_language=None,
            )
        ]
