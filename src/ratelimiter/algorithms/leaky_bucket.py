"""
Leaky Bucket rate-limiting algorithm.

Requests fill a "bucket" of finite capacity.  The bucket drains at a
constant *leak_rate* (requests per second).  Excess requests are
rejected immediately (the "leaky bucket as a meter" variant, which is
more useful for rate limiting than the queue variant).

Pros:  Perfectly smooth output rate; no bursty traffic permitted beyond
       bucket capacity.
Cons:  No burst allowance once the bucket is full; latency-sensitive.
"""

from __future__ import annotations

import time
from typing import Optional

from ..backends.base import BaseBackend
from .base import BaseAlgorithm, RateLimitResult


class LeakyBucketRateLimiter(BaseAlgorithm):
    """Leaky bucket (as a meter) rate limiter.

    Args:
        backend:   Storage backend.
        limit:     Bucket capacity (maximum inflight requests).
        window:    Time to drain a full bucket in seconds.
                   ``leak_rate = limit / window`` per second.
        leak_rate: Override drain rate (requests per second).

    Example::

        from ratelimiter.backends.memory import MemoryBackend
        from ratelimiter.algorithms.leaky_bucket import LeakyBucketRateLimiter

        limiter = LeakyBucketRateLimiter(MemoryBackend(), limit=100, window=10)
        result = limiter.is_allowed("user:7")
    """

    def __init__(
        self,
        backend: BaseBackend,
        limit: int,
        window: float,
        leak_rate: Optional[float] = None,
        key_prefix: str = "",
        config_provider=None,
    ) -> None:
        super().__init__(backend, limit, window, key_prefix, config_provider)
        self.leak_rate: float = leak_rate if leak_rate is not None else limit / window

    def _state_key(self, key: str) -> str:
        return f"{self._full_key(key)}:lb"

    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        self._refresh_config()
        now = time.time()
        state_key = self._state_key(key)

        raw = self.backend.get(state_key)

        if raw is None:
            level: float = 0.0
            last_leak = now
        else:
            level = float(raw["level"])
            last_leak = float(raw["last_leak"])

        # Drain the bucket proportional to elapsed time
        elapsed = now - last_leak
        level = max(0.0, level - elapsed * self.leak_rate)

        allowed = (level + cost) <= self.limit

        if allowed:
            level += cost

        self.backend.set(
            state_key,
            {"level": level, "last_leak": now},
            ttl=self.window * 2,
        )

        remaining = max(0, int(self.limit - level))

        # Time until `cost` units can fit into the bucket
        if allowed:
            retry_after = 0.0
        else:
            overflow = (level + cost) - self.limit
            retry_after = overflow / self.leak_rate

        # Time until bucket is empty
        time_to_empty = level / self.leak_rate if self.leak_rate > 0 else 0.0

        return RateLimitResult(
            allowed=allowed,
            key=key,
            limit=self.limit,
            remaining=remaining,
            reset_after=time_to_empty,
            retry_after=retry_after,
            metadata={"level": level, "leak_rate": self.leak_rate},
        )

    def reset(self, key: str) -> None:
        """Clear leaky bucket state for *key*."""
        self.backend.delete(self._state_key(key))
