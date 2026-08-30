"""Test rate limiting con store condiviso (WP-E1, GE1)."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.rate_limit import (
    InMemoryRateLimitStore,
    RateLimiter,
    RedisRateLimitStore,
)


class MockStore:
    """Store di test: registra le chiamate e restituisce un esito fisso."""

    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.calls: list[tuple[str, float, float, float]] = []

    def consume(
        self, key: str, tokens: float, refill_rate: float, max_tokens: float
    ) -> bool:
        self.calls.append((key, tokens, refill_rate, max_tokens))
        return self.allowed

    def clear(self) -> None:
        self.calls.clear()


def test_rate_limiter_uses_shared_store() -> None:
    store = MockStore(allowed=True)
    limiter = RateLimiter(default_limit=20.0, auth_limit=5.0, store=store)
    assert limiter.is_allowed("1.2.3.4", is_auth_endpoint=True) is True
    assert store.calls == [("1.2.3.4", 1.0, 5.0, 10.0)]


def test_rate_limiter_raises_429_when_store_denies() -> None:
    store = MockStore(allowed=False)
    limiter = RateLimiter(store=store)
    with pytest.raises(HTTPException) as exc_info:
        limiter.check_rate_limit("1.2.3.4")
    assert exc_info.value.status_code == 429


def test_in_memory_store_backward_compatible_buckets() -> None:
    limiter = RateLimiter(default_limit=10.0, auth_limit=5.0)
    assert isinstance(limiter._store, InMemoryRateLimitStore)
    limiter._buckets.clear()
    assert limiter.is_allowed("1.2.3.4") is True


def test_redis_store_requires_url() -> None:
    with pytest.raises(RuntimeError):
        RedisRateLimitStore(url="")
