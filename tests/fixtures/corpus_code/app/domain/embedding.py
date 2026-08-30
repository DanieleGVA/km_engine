"""Deterministic offline embeddings for the RAG retrieval layer (WP-B1).

The vector index on ``Document.embedding`` (384-dim, cosine) already exists
(ADR-004 / ``db/neo4j/002_domain_schema.cypher``). This module provides the
embedding services that populate and query it.

Design constraints:

- **Deterministic**: no network, no random state, no LLM. The same text always
  produces the same vector, across processes and machines.
- **384 dimensions**: the vector index is fixed at 384 dimensions.
- **Cosine-meaningful**: vectors are L2-normalised and built from non-negative
  TF-IDF-like weights, so cosine similarity behaves like a weighted token
  overlap.
- **Vocabulary from pack + corpus**: :class:`DeterministicEmbedding` is built
  from a collection of reference texts (the Domain Pack glossaries and/or the
  ingested corpus). IDF is estimated from those texts; unknown tokens fall back
  to a deterministic default IDF so a query and a document always use the same
  weighting rule.
"""
from __future__ import annotations

import hashlib
import itertools
import math
import re
from typing import Protocol, runtime_checkable

DIMENSIONS = 384

# Word tokens: Unicode letters/numbers, with an optional internal apostrophe
# (``l'aglio``, ``d'oliva``). Accented letters are covered by ``[^\W_]``.
_TOKEN_RE = re.compile(r"[^\W_]+(?:['\u2019][^\W_]+)?")


@runtime_checkable
class EmbeddingService(Protocol):
    """Protocol for a 384-dimension text embedding service."""

    def embed(self, text: str) -> list[float]:
        """Return a 384-dimension embedding for ``text``."""
        ...


def _tokenize(text: str) -> list[str]:
    """Return lowercased unigrams and adjacent bigrams for ``text``.

    Bigrams are included so short phrases (``peeled tomatoes``,
    ``olive oil``) survive the hashing projection better than with unigrams
    alone. The token stream is deterministic.
    """
    words = _TOKEN_RE.findall(text.casefold())
    tokens: list[str] = list(words)
    tokens.extend(f"{left} {right}" for left, right in itertools.pairwise(words))
    return tokens


def _stable_hash(token: str) -> int:
    """Deterministic 64-bit hash (Python ``hash()`` is salted per process)."""
    digest = hashlib.sha256(token.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _idf(n_docs: int, doc_freq: int) -> float:
    """Smooth inverse document frequency (never zero, deterministic)."""
    return math.log((n_docs + 1.0) / (doc_freq + 1.0)) + 1.0


class DeterministicEmbedding:
    """TF-IDF-like hashing embedder over a fixed vocabulary.

    The vocabulary is a ``token -> idf`` mapping estimated from the reference
    texts passed to :meth:`from_texts`. Embedding projects the weighted token
    stream into exactly :data:`DIMENSIONS` buckets with signed feature hashing
    and L2-normalises the result.
    """

    def __init__(self, idf: dict[str, float], default_idf: float = 1.0) -> None:
        self._idf = dict(idf)
        self._default_idf = default_idf

    @classmethod
    def from_texts(cls, texts: list[str]) -> DeterministicEmbedding:
        """Build the vocabulary from reference texts (pack + corpus)."""
        n_docs = len(texts)
        doc_freq: dict[str, int] = {}
        for text in texts:
            for token in set(_tokenize(text)):
                doc_freq[token] = doc_freq.get(token, 0) + 1
        idf = {
            token: _idf(n_docs, freq) for token, freq in doc_freq.items()
        }
        return cls(idf)

    @classmethod
    def from_pack(cls, pack) -> DeterministicEmbedding:
        """Build the vocabulary from a Domain Pack glossary (offline, stable)."""
        texts: list[str] = []
        for entry in pack.glossary_entries():
            parts = [
                entry.labels_en,
                entry.labels_it,
                *entry.aliases,
                entry.definition,
            ]
            texts.append(" ".join(part for part in parts if part))
        return cls.from_texts(texts)

    def _weight(self, token: str) -> float:
        return self._idf.get(token, self._default_idf)

    def embed(self, text: str) -> list[float]:
        """Return a 384-dimension L2-normalised embedding for ``text``."""
        vector = [0.0] * DIMENSIONS
        counts: dict[str, int] = {}
        for token in _tokenize(text):
            counts[token] = counts.get(token, 0) + 1

        for token, count in counts.items():
            # Sublinear TF keeps very frequent tokens from dominating.
            weight = (1.0 + math.log(count)) * self._weight(token)
            h = _stable_hash(token)
            bucket = h % DIMENSIONS
            sign = 1.0 if (h >> 1) & 1 else -1.0
            vector[bucket] += sign * weight

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            # Deterministic fallback for text with no known tokens: a single
            # fixed bucket keeps the vector non-zero and cosine-safe.
            vector[_stable_hash("__unknown__") % DIMENSIONS] = 1.0
            return vector
        return [value / norm for value in vector]


class HttpEmbeddingService:
    """Skeleton for a real HTTP embedding service (never used in tests).

    Configuration is read from the environment:

    - ``KM_EMBEDDING_ENDPOINT``: base URL of the embedding API
    - ``KM_EMBEDDING_API_KEY``: optional bearer API key
    - ``KM_EMBEDDING_MODEL``: model name sent to the API

    The implementation is intentionally not wired to the network in this
    iteration: tests and the deterministic golden set use
    :class:`DeterministicEmbedding`. When a hosted embedding model is adopted
    (WP-B5/industrialisation), implement :meth:`embed` with ``httpx`` and keep
    the :class:`EmbeddingService` protocol as the only contract the RAG layer
    depends on.
    """

    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        import os

        self.endpoint = endpoint or os.getenv("KM_EMBEDDING_ENDPOINT", "")
        self.api_key = api_key or os.getenv("KM_EMBEDDING_API_KEY", "")
        self.model = model or os.getenv("KM_EMBEDDING_MODEL", "")

    def embed(self, text: str) -> list[float]:
        """Not implemented: raise an explicit error (no network in tests)."""
        raise NotImplementedError(
            "HttpEmbeddingService is a documented skeleton; "
            "use DeterministicEmbedding for offline retrieval."
        )
