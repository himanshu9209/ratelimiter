"""
Token Bucket rate-limiting algorithm.

A bucket holds up to ``capacity`` tokens.  Tokens are added at a constant
``refill_rate`` (tokens per second) up to the bucket's capacity.  Each
request consumes one or more tokens.  If the bucket is empty the request
is rejected.

Pros:  Handles bursty traffic elegantly (up to ``capacity`` tokens at
       once); smooth average throughput.
Cons:  Slightly more complex state (tokens + last_refill timestamp).
"""

from __future__ import annotations

import time
from typing import Optional

from ..backends.base import BaseBackend
from .base import BaseAlgorithm, RateLimitResult


class TokenBucketRateLimiter(BaseAlgorithm):
    """Token bucket rate limiter.

    Args:
        backend:     Storage backend.
        limit:       Bucket capacity (maximum burst size).
        window:      Time in seconds to fully refill an empty bucket.
                     ``refill_rate = limit / window`` tokens per second.
        refill_rate: Override tokens-per-second refill rate.
                     If supplied, ``window`` is ignored for refill purposes
                     but still used for TTL management.

    Example::

        from ratelimiter.backends.memory import MemoryBackend
        from ratelimiter.algorithms.token_bucket import TokenBucketRateLimiter

        # 50 req/s with burst up to 200
        limiter = TokenBucketRateLimiter(
            MemoryBackend(), limit=200, window=60, refill_rate=50
        )
        result = limiter.is_allowed("api_key:abc")
    """

    def __init__(
        self,
        backend: BaseBackend,
        limit: int,
        window: float,
        refill_rate: Optional[float] = None,
        key_prefix: str = "",
    ) -> None:
        super().__init__(backend, limit, window, key_prefix)
        self.refill_rate: float = refill_rate if refill_rate is not None else limit / window

    def _state_key(self, key: str) -> str:
        return f"{self._full_key(key)}:tb"

    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        now = time.time()
        state_key = self._state_key(key)

        raw = self.backend.get(state_key)

        if raw is None:
            tokens = float(self.limit)
            last_refill = now
        else:
            tokens, last_refill = float(raw["tokens"]), float(raw["last_refill"])

        # Refill tokens proportional to elapsed time
        elapsed = now - last_refill
        tokens = min(float(self.limit), tokens + elapsed * self.refill_rate)

        allowed = tokens >= cost
        if allowed:
            tokens -= cost

        self.backend.set(
            state_key,
            {"tokens": tokens, "last_refill": now},
            ttl=self.window * 2,
        )

        remaining = int(tokens)
        # How long until *cost* tokens are available?
        if allowed:
            retry_after = 0.0
        else:
            deficit = cost - tokens
            retry_after = deficit / self.refill_rate

        time_to_full = (self.limit - tokens) / self.refill_rate

        return RateLimitResult(
            allowed=allowed,
            key=key,
            limit=self.limit,
            remaining=remaining,
            reset_after=time_to_full,
            retry_after=retry_after,
            metadata={"tokens": tokens, "refill_rate": self.refill_rate},
        )

    def reset(self, key: str) -> None:
        """Clear token bucket state for *key*."""
        self.backend.delete(self._state_key(key))
