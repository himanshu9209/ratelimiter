"""
Sliding Window Counter rate-limiting algorithm.

Combines the memory efficiency of Fixed Window (O(1) storage) with the
accuracy of Sliding Window Log by blending two adjacent fixed-window
counters using a weighted approximation.

How it works
------------
Two counters are maintained: one for the **current** window bucket and
one for the **previous** bucket.  The effective count is:

    effective = prev_count × (1 - elapsed / window) + curr_count

where ``elapsed`` is the time since the current window started.  As the
window slides forward, the previous bucket's contribution fades linearly
from 100 % down to 0 %.

Compared to Sliding Window Log
-------------------------------
* **Memory**: O(1) — two counters per key regardless of traffic volume.
  The log stores one entry per request (O(N)).
* **Accuracy**: ~98–99 % in typical traffic patterns.  The approximation
  can be slightly off at window boundaries, but the error is bounded and
  rarely noticeable in practice.
* **Burst safety**: Still prevents the 2× boundary burst that Fixed
  Window suffers from.

Use this when memory is a concern and you can tolerate a small
approximation error.  Use :class:`~ratelimiter.algorithms.sliding_window.SlidingWindowRateLimiter`
when you need exact counts.

Example::

    from smart_ratelimiter.backends.memory import MemoryBackend
    from smart_ratelimiter.algorithms.sliding_window_counter import SlidingWindowCounterRateLimiter

    limiter = SlidingWindowCounterRateLimiter(MemoryBackend(), limit=100, window=60)
    result = limiter.is_allowed("user:42")
    if not result.allowed:
        print(f"Retry in {result.retry_after:.1f}s")
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Optional

from ..backends.base import BaseBackend
from .base import BaseAlgorithm, RateLimitResult

if TYPE_CHECKING:
    from ..config import ConfigProvider


class SlidingWindowCounterRateLimiter(BaseAlgorithm):
    """Sliding window counter rate limiter (memory-efficient approximation).

    Uses two adjacent fixed-window counters and a linear interpolation to
    approximate a true sliding window.  O(1) memory per key.

    Args:
        backend:    Storage backend.
        limit:      Maximum requests per window.
        window:     Window duration in seconds.
        key_prefix: Optional key namespace.

    Example::

        limiter = SlidingWindowCounterRateLimiter(
            MemoryBackend(), limit=60, window=60
        )
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

    def _bucket_key(self, key: str, bucket_id: int) -> str:
        """Return the storage key for a fixed-window bucket."""
        return f"{self._full_key(key)}:swc:{bucket_id}"

    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        self._refresh_config()
        now = time.time()

        # Identify the current and previous fixed-window buckets
        window = self.window
        curr_bucket_id = int(now / window)
        prev_bucket_id = curr_bucket_id - 1

        # How far we are into the current bucket (0.0 → 1.0)
        elapsed = now - (curr_bucket_id * window)
        weight_prev = 1.0 - (elapsed / window)   # previous window's remaining share

        curr_key = self._bucket_key(key, curr_bucket_id)
        prev_key = self._bucket_key(key, prev_bucket_id)

        curr_count = self.backend.get(curr_key) or 0
        prev_count = self.backend.get(prev_key) or 0

        # Weighted approximation of requests in the virtual sliding window
        effective_count = prev_count * weight_prev + curr_count

        allowed = (effective_count + cost) <= self.limit

        if allowed:
            # Increment the current bucket and (re-)set its TTL
            new_curr = self.backend.incr(curr_key, cost)
            if new_curr == cost:
                # First write to this bucket — set TTL for two full windows
                # so the previous bucket is still readable when we need it
                self.backend.expire(curr_key, window * 2)

        remaining = max(0, int(self.limit - effective_count - (cost if allowed else 0)))

        # How long until enough slots free up (linear interpolation)
        if allowed:
            retry_after = 0.0
            reset_after = window - elapsed
        else:
            # Estimate time until effective_count drops below limit
            # effective_count decreases as weight_prev shrinks.
            # Solve: prev_count * (1 - t/window) + curr_count <= limit
            # => t >= window * (1 - (limit - curr_count) / prev_count)
            if prev_count > 0:
                needed_reduction = effective_count - self.limit
                # time for prev contribution to drop by needed_reduction:
                # prev_count * delta_weight = needed_reduction
                # delta_weight = needed_reduction / prev_count
                # delta_t = delta_weight * window
                delta_t = (needed_reduction / prev_count) * window
                retry_after = max(0.0, delta_t - elapsed)
            else:
                retry_after = window - elapsed
            reset_after = window - elapsed

        return RateLimitResult(
            allowed=allowed,
            key=key,
            limit=self.limit,
            remaining=remaining,
            reset_after=reset_after,
            retry_after=retry_after,
            metadata={
                "curr_count": int(curr_count),
                "prev_count": int(prev_count),
                "effective_count": round(effective_count, 3),
                "weight_prev": round(weight_prev, 3),
            },
        )

    def reset(self, key: str) -> None:
        """Clear both window buckets for *key*."""
        now = time.time()
        curr_bucket_id = int(now / self.window)
        self.backend.delete(self._bucket_key(key, curr_bucket_id))
        self.backend.delete(self._bucket_key(key, curr_bucket_id - 1))
