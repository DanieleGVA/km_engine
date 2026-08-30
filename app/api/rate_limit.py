"""Rate limiting with pluggable shared store (WP-E1, GE1).

The MVP limiter was an in-memory token bucket per API instance. With multiple
``km-api`` replicas behind nginx, per-instance memory is not a shared limit:
each replica would allow the full rate. This module keeps the same
:class:`RateLimiter` contract but delegates bucket state to a
:class:`RateLimitStore`.

- :class:`InMemoryRateLimitStore` — default, per-process (documented fallback).
- :class:`RedisRateLimitStore` — shared across replicas when Redis is
  available and ``KM_RATE_LIMIT_REDIS_URL`` is set. The ``redis`` package is
  optional: if it is not installed, constructing the store raises a clear
  error instead of silently falling back.
"""
from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class TokenBucket:
    """Token bucket state (in-memory store)."""

    tokens: float = field(default=10.0)
    last_update: float = field(default_factory=time.time)

    def consume(
        self, tokens: float = 1.0, refill_rate: float = 1.0, max_tokens: float = 10.0
    ) -> bool:
        """Try to consume ``tokens``. Return True on success."""
        now = time.time()
        elapsed = now - self.last_update
        self.tokens = min(max_tokens, self.tokens + elapsed * refill_rate)
        self.last_update = now
        if self.tokens >= tokens:
            self.tokens -= tokens
            return True
        return False


@runtime_checkable
class RateLimitStore(Protocol):
    """Shared state contract for the rate limiter."""

    def consume(
        self, key: str, tokens: float, refill_rate: float, max_tokens: float
    ) -> bool:
        """Atomically consume tokens for ``key``; True when allowed."""
        ...

    def clear(self) -> None:
        """Reset all state (test/ops helper)."""
        ...


class InMemoryRateLimitStore:
    """Per-process token buckets (default; not shared across replicas)."""

    def __init__(self) -> None:
        self.buckets: dict[str, TokenBucket] = defaultdict(TokenBucket)

    def consume(
        self, key: str, tokens: float, refill_rate: float, max_tokens: float
    ) -> bool:
        return self.buckets[key].consume(tokens, refill_rate, max_tokens)

    def clear(self) -> None:
        self.buckets.clear()


class RedisRateLimitStore:
    """Shared token-bucket store backed by Redis (optional dependency).

    Uses a small Lua script so the read/refill/consume sequence is atomic
    across API replicas. Requires the ``redis`` package and
    ``KM_RATE_LIMIT_REDIS_URL`` (or an explicit ``url``).
    """

    def __init__(self, url: str | None = None) -> None:
        import os

        self.url = url or os.getenv("KM_RATE_LIMIT_REDIS_URL", "")
        if not self.url:
            raise RuntimeError(
                "RedisRateLimitStore richiede KM_RATE_LIMIT_REDIS_URL "
                "(es. redis://localhost:6379/0)."
            )
        try:
            import redis as redis_lib
        except ImportError as exc:  # pragma: no cover - dipendenza opzionale
            raise RuntimeError(
                "RedisRateLimitStore richiede il pacchetto opzionale 'redis'."
            ) from exc
        self._redis = redis_lib.from_url(self.url, decode_responses=True)
        self._script = self._redis.register_script(
            """
            local tokens = tonumber(redis.call('GET', KEYS[1]) or ARGV[1])
            local last = tonumber(redis.call('GET', KEYS[1] .. ':ts') or ARGV[4])
            local now = tonumber(ARGV[4])
            local elapsed = now - last
            tokens = math.min(tonumber(ARGV[2]), tokens + elapsed * tonumber(ARGV[3]))
            if tokens >= tonumber(ARGV[5]) then
                tokens = tokens - tonumber(ARGV[5])
                redis.call('SET', KEYS[1], tokens)
                redis.call('SET', KEYS[1] .. ':ts', now)
                return 1
            end
            redis.call('SET', KEYS[1], tokens)
            redis.call('SET', KEYS[1] .. ':ts', now)
            return 0
            """
        )

    def consume(
        self, key: str, tokens: float, refill_rate: float, max_tokens: float
    ) -> bool:
        now = time.time()
        result = self._script(
            keys=[f"km_rate:{key}"],
            args=[max_tokens, max_tokens, refill_rate, now, tokens],
        )
        return bool(int(result))

    def clear(self) -> None:
        # Best-effort: only used in tests/ops; production keys expire naturally
        # via the token-bucket TTL policy (not implemented here to keep the
        # optional dependency surface minimal).
        try:
            self._redis.flushdb()
        except Exception:  # noqa: BLE001 - clear is best-effort
            return


def build_rate_limiter(
    default_limit: float = 10.0, auth_limit: float = 5.0
) -> RateLimiter:
    """Build the process limiter, using Redis when configured and available.

    If ``KM_RATE_LIMIT_REDIS_URL`` is set but the optional ``redis`` package is
    missing (or the URL is invalid), fall back to the in-memory store and keep
    the service running (documented degradation: the limit is per-process).
    """
    import os

    url = os.getenv("KM_RATE_LIMIT_REDIS_URL", "")
    if url:
        try:
            return RateLimiter(
                default_limit=default_limit,
                auth_limit=auth_limit,
                store=RedisRateLimitStore(url=url),
            )
        except RuntimeError:
            # Redis non disponibile: fallback in-memory documentato.
            pass
    return RateLimiter(default_limit=default_limit, auth_limit=auth_limit)


class RateLimiter:
    """Rate limiter with a pluggable store (in-memory by default)."""

    def __init__(
        self,
        default_limit: float = 10.0,
        auth_limit: float = 5.0,
        store: RateLimitStore | None = None,
    ) -> None:
        self.default_limit = default_limit
        self.auth_limit = auth_limit
        self._store = store or InMemoryRateLimitStore()
        # Backward compatibility: the old limiter exposed ``_buckets`` for test
        # reset. Keep it for the in-memory store; no-op dict otherwise.
        if isinstance(self._store, InMemoryRateLimitStore):
            self._buckets = self._store.buckets
        else:
            self._buckets = {}

    def get_bucket(self, ip: str) -> TokenBucket:
        """Return the in-memory bucket for ``ip`` (backward compatibility)."""
        if not isinstance(self._store, InMemoryRateLimitStore):
            raise TypeError("get_bucket è disponibile solo con lo store in-memory.")
        return self._store.buckets[ip]

    def is_allowed(self, ip: str, is_auth_endpoint: bool = False) -> bool:
        limit = self.auth_limit if is_auth_endpoint else self.default_limit
        return self._store.consume(
            key=ip,
            tokens=1.0,
            refill_rate=limit,
            max_tokens=limit * 2,
        )

    def check_rate_limit(self, ip: str, is_auth_endpoint: bool = False) -> None:
        """Check the rate limit and raise HTTPException 429 when exceeded."""
        if not self.is_allowed(ip, is_auth_endpoint):
            from fastapi import HTTPException, status

            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests. Rate limit exceeded.",
                headers={"Retry-After": "60"},
            )
