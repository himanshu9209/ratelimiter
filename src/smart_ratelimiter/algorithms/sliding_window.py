"""
Sliding Window Log rate-limiting algorithm.

A sorted set stores the timestamp of every request in the last ``window``
seconds.  Stale entries are pruned on each check.  This gives perfect
accuracy at the cost of O(N) memory per key (where N = limit).

Pros:  No boundary burst problem; most accurate algorithm available.
Cons:  Higher memory usage — stores one entry per request.
"""

from __future__ import annotations

import itertools
import time
from typing import TYPE_CHECKING, Optional

_counter = itertools.count()

from ..backends.base import BaseBackend
from .base import BaseAlgorithm, RateLimitResult

if TYPE_CHECKING:
    from ..config import ConfigProvider


class SlidingWindowRateLimiter(BaseAlgorithm):
    """Sliding window log rate limiter.

    Uses a sorted set keyed by timestamp so only requests inside the
    rolling window are counted.

    Example::

        from smart_ratelimiter.backends.memory import MemoryBackend
        from smart_ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter

        limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=10, window=1.0)
        result = limiter.is_allowed("192.168.1.1")
    """

    def __init__(
        self,
        backend: BaseBackend,
        limit: int,
        window: float,
        key_prefix: str = "",
        config_provider: Optional["ConfigProvider"] = None,
    ) -> None:
        super().__init__(backend, limit, window, key_prefix, config_provider)

    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        self._refresh_config()
        now = time.time()
        window_start = now - self.window
        full_key = self._full_key(key)

        # 1. Remove timestamps outside the current window
        self.backend.zremrangebyscore(full_key, 0, window_start)

        # 2. Count requests still in the window
        current_count = self.backend.zcard(full_key)

        allowed = (current_count + cost) <= self.limit

        if allowed:
            # 3. Record each unit of cost as a separate timestamped entry
            for _ in range(cost):
                member = f"{now}:{next(_counter)}"
                self.backend.zadd(full_key, now, member)
            # Keep the sorted set alive for at least one full window
            self.backend.expire(full_key, self.window + 1)

        remaining = max(0, self.limit - current_count - (cost if allowed else 0))

        # Oldest entry in the window tells us when a slot will free up
        oldest = self.backend.zrange_by_score(full_key, window_start, now)
        if oldest:
            oldest_ts = oldest[0][1]
            retry_after = (oldest_ts + self.window) - now
        else:
            retry_after = 0.0

        return RateLimitResult(
            allowed=allowed,
            key=key,
            limit=self.limit,
            remaining=remaining,
            reset_after=self.window,
            retry_after=max(0.0, retry_after) if not allowed else 0.0,
            metadata={"current_count": current_count},
        )
