"""Unit tests for the deterministic embedding service (WP-B1)."""
from __future__ import annotations

import math

import pytest

from app.domain.embedding import (
    DIMENSIONS,
    DeterministicEmbedding,
    EmbeddingService,
    HttpEmbeddingService,
)


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(value * value for value in vector))


def test_ib_embedding_is_384d_and_normalised() -> None:
    service = DeterministicEmbedding.from_texts(["spaghetti garlic basil"])
    vector = service.embed("spaghetti with garlic and basil")
    assert len(vector) == DIMENSIONS
    assert _norm(vector) == pytest.approx(1.0)


def test_ib_embedding_is_deterministic() -> None:
    service = DeterministicEmbedding.from_texts(["spaghetti garlic basil"])
    assert service.embed("spaghetti garlic") == service.embed("spaghetti garlic")


def test_ib_embedding_cosine_meaningful() -> None:
    service = DeterministicEmbedding.from_texts(
        ["spaghetti tomato garlic", "risotto saffron butter", "apple cake sugar"]
    )
    a = service.embed("spaghetti tomato garlic")
    b = service.embed("spaghetti tomato garlic")
    c = service.embed("risotto saffron butter")
    assert _cosine(a, b) == pytest.approx(1.0)
    assert _cosine(a, c) < _cosine(a, b)


def test_ib_embedding_unknown_text_has_nonzero_fallback() -> None:
    service = DeterministicEmbedding.from_texts(["spaghetti garlic"])
    vector = service.embed("???" )
    assert _norm(vector) == pytest.approx(1.0)


def test_ib_embedding_protocol_and_http_skeleton() -> None:
    assert isinstance(DeterministicEmbedding.from_texts(["x"]), EmbeddingService)
    http = HttpEmbeddingService(endpoint="http://example.test", model="m")
    assert http.endpoint == "http://example.test"
    assert http.model == "m"
    with pytest.raises(NotImplementedError):
        http.embed("never called in tests")


def _cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
