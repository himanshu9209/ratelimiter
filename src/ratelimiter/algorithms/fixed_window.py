"""
Fixed Window rate-limiting algorithm.

The time axis is divided into fixed, non-overlapping buckets of ``window``
seconds each.  A counter is incremented for every request in the current
bucket; once it reaches ``limit`` the request is rejected until the bucket
resets.

Pros:  Very cheap — a single INCR + EXPIRE per request.
Cons:  Boundary burst problem: a caller can make 2× the limit by sending
       ``limit`` requests just before the window rolls over and another
       ``limit`` immediately after.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from ..backends.base import BaseBackend
from .base import BaseAlgorithm, RateLimitResult

if TYPE_CHECKING:
    from ..config import ConfigProvider


class FixedWindowRateLimiter(BaseAlgorithm):
    """Fixed window counter rate limiter.

    Args:
        backend: Storage backend.
        limit:   Maximum requests per window.
        window:  Window duration in seconds.

    Example::

        from ratelimiter.backends.memory import MemoryBackend
        from ratelimiter.algorithms.fixed_window import FixedWindowRateLimiter

        limiter = FixedWindowRateLimiter(MemoryBackend(), limit=100, window=60)
        result = limiter.is_allowed("user:42")
        if not result.allowed:
            raise TooManyRequests(retry_after=result.retry_after)
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
        # Snap to the start of the current window
        window_start = int(now / self.window) * self.window
        bucket_key = f"{self._full_key(key)}:{int(window_start)}"

        count = self.backend.incr(bucket_key, cost)
        if count == cost:
            # First request in this window — set the TTL
            self.backend.expire(bucket_key, self.window)

        window_end = window_start + self.window
        reset_after = window_end - now
        remaining = max(0, self.limit - count)
        allowed = count <= self.limit

        return RateLimitResult(
            allowed=allowed,
            key=key,
            limit=self.limit,
            remaining=remaining,
            reset_after=reset_after,
            retry_after=0.0 if allowed else reset_after,
            metadata={"window_start": window_start, "count": count},
        )

    def reset(self, key: str) -> None:
        """Clear the current window counter for *key*."""
        now = time.time()
        window_start = int(now / self.window) * self.window
        bucket_key = f"{self._full_key(key)}:{int(window_start)}"
        self.backend.delete(bucket_key)
