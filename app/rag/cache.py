"""In-process TTL caches for the RAG layer (WP-B5, gate GB5).

Three caches remove the per-request graph round-trips that dominated
retrieval latency in the MVP baseline:

- ``vocab_cache``: the deterministic embedding vocabulary built by
  :func:`app.rag.rag.build_embedding_from_graph` (pack + corpus). Rebuilt
  only when the TTL expires or when ingest invalidates it.
- ``recompose_cache``: ``canonical_md`` per ``:Document`` (the recomposer
  runs 3 graph queries per document).
- ``context_cache``: graph expansion (entities/terms/provenance) per
  ``:Document`` (2 graph queries per document).

Invalidation: :func:`invalidate_rag_caches` is called by
``extract_document`` (ingest) and ``populate_embeddings`` so a fresh
vocabulary/context is picked up immediately; the TTL bounds staleness for
any other graph mutation (e.g. fact invalidation).

The module is deliberately dependency-free (stdlib only) so the domain
layer (``app/domain/extract.py``, ``app/domain/recompose.py``) can
invalidate the caches without importing the RAG retrieval code.

TTL is configurable via ``KM_RAG_CACHE_TTL`` (seconds, default 300).
``set_cache_ttl(0)`` disables caching (used by the ``-m perf``
micro-benchmark to measure the pre-optimisation baseline).
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Generic, TypeVar

T = TypeVar("T")

DEFAULT_TTL = float(os.getenv("KM_RAG_CACHE_TTL", "300"))


class TTLCache(Generic[T]):
    """Thread-safe TTL cache. ``ttl <= 0`` disables caching."""

    def __init__(self, ttl: float = DEFAULT_TTL) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._items: dict[Any, tuple[float, T]] = {}

    @property
    def ttl(self) -> float:
        return self._ttl

    def set_ttl(self, ttl: float) -> None:
        with self._lock:
            self._ttl = ttl
            if ttl <= 0:
                self._items.clear()

    def get(self, key: Any) -> T | None:
        if self._ttl <= 0:
            return None
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            expires_at, value = item
            if time.monotonic() > expires_at:
                del self._items[key]
                return None
            return value

    def set(self, key: Any, value: T) -> None:
        if self._ttl <= 0:
            return
        with self._lock:
            self._items[key] = (time.monotonic() + self._ttl, value)

    def clear(self) -> None:
        with self._lock:
            self._items.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


vocab_cache: TTLCache[Any] = TTLCache()
recompose_cache: TTLCache[str] = TTLCache()
context_cache: TTLCache[dict[str, Any]] = TTLCache()


def invalidate_rag_caches() -> None:
    """Drop all RAG caches (called on ingest and embedding population)."""
    vocab_cache.clear()
    recompose_cache.clear()
    context_cache.clear()


def set_cache_ttl(ttl: float) -> None:
    """Set the TTL for all RAG caches (``0`` disables caching)."""
    vocab_cache.set_ttl(ttl)
    recompose_cache.set_ttl(ttl)
    context_cache.set_ttl(ttl)
