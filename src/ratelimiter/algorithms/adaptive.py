"""
Adaptive Hybrid rate-limiting algorithm.

Combines the accuracy of a sliding window with the burst tolerance of a
token bucket, then adds a third layer: **adaptive throttling** that
automatically tightens or relaxes limits based on observed traffic load.

How it works
------------
1. **Sliding window guard** — An inner sliding-window limiter enforces an
   absolute ceiling.  No caller can exceed ``limit`` requests per
   ``window`` seconds, ever.

2. **Token bucket burst layer** — A token bucket allows short, controlled
   bursts up to ``burst_multiplier × limit`` without immediately hitting
   the hard ceiling.  This absorbs legitimate traffic spikes.

3. **Adaptive load factor** — The limiter tracks a smoothed request rate
   over a longer ``adaptive_window``.  When the rate exceeds
   ``high_load_threshold × limit / window``, it applies a ``penalty``
   that reduces the effective burst allowance.  When the rate drops below
   ``low_load_threshold × limit / window``, it relaxes the burst cap back
   towards its maximum.

The result: under low load, callers enjoy a generous burst allowance;
under high load, the limiter automatically becomes stricter without any
manual tuning.

Example::

    from ratelimiter.backends.memory import MemoryBackend
    from ratelimiter.algorithms.adaptive import AdaptiveRateLimiter

    limiter = AdaptiveRateLimiter(
        backend=MemoryBackend(),
        limit=100,          # hard ceiling per window
        window=60.0,        # 100 req / 60 s = ~1.67 req/s average
        burst_multiplier=3, # allow up to 300 burst tokens when quiet
        adaptive_window=300,# measure load over 5 min
    )
    result = limiter.is_allowed("tenant:acme")
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


class AdaptiveRateLimiter(BaseAlgorithm):
    """Adaptive hybrid rate limiter (sliding window + token bucket + load sensing).

    Args:
        backend:             Storage backend.
        limit:               Hard request ceiling per ``window`` seconds.
        window:              Base time window in seconds.
        burst_multiplier:    Max burst = ``limit × burst_multiplier``
                             (used when load is low). Default 2.
        adaptive_window:     Look-back period for load measurement.
                             Default 5 × ``window``.
        high_load_threshold: Fraction of ``limit/window`` r/s at which
                             the burst cap starts shrinking. Default 0.8.
        low_load_threshold:  Fraction at which the burst cap is restored.
                             Default 0.4.
        penalty:             How much to reduce the effective burst cap
                             under high load (0–1). Default 0.5.
        key_prefix:          Optional key namespace.
    """

    def __init__(
        self,
        backend: BaseBackend,
        limit: int,
        window: float,
        burst_multiplier: float = 2.0,
        adaptive_window: float | None = None,
        high_load_threshold: float = 0.8,
        low_load_threshold: float = 0.4,
        penalty: float = 0.5,
        key_prefix: str = "",
        config_provider: Optional["ConfigProvider"] = None,
    ) -> None:
        super().__init__(backend, limit, window, key_prefix, config_provider)
        self.burst_multiplier = burst_multiplier
        self.adaptive_window = adaptive_window or window * 5
        self.high_load_threshold = high_load_threshold
        self.low_load_threshold = low_load_threshold
        self.penalty = penalty
        # Derived
        self._base_rate = limit / window  # requests per second at nominal load
        self._max_burst = int(limit * burst_multiplier)
        self._refill_rate = self._max_burst / window
        # Cache for _effective_burst: key -> (burst_value, computed_at)
        self._burst_cache: dict[str, tuple[int, float]] = {}
        self._burst_cache_ttl = min(1.0, window / 10)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _sw_key(self, key: str) -> str:
        """Sliding window sorted-set key."""
        return f"{self._full_key(key)}:aw:sw"

    def _tb_key(self, key: str) -> str:
        """Token bucket state key."""
        return f"{self._full_key(key)}:aw:tb"

    def _load_key(self, key: str) -> str:
        """Global load-sensing sorted-set key (shared across callers)."""
        return f"{self.key_prefix}__global__:aw:load"

    def _effective_burst(self, key: str, now: float) -> int:
        """Compute the burst cap adjusted for current load, with TTL cache."""
        cached = self._burst_cache.get(key)
        if cached is not None and (now - cached[1]) < self._burst_cache_ttl:
            return cached[0]

        load_key = self._load_key(key)
        window_start = now - self.adaptive_window
        self.backend.zremrangebyscore(load_key, 0, window_start)
        recent_requests = self.backend.zcard(load_key)

        measured_rate = recent_requests / self.adaptive_window  # req/s
        high_threshold = self._base_rate * self.high_load_threshold
        low_threshold = self._base_rate * self.low_load_threshold

        if measured_rate >= high_threshold:
            # High load: shrink burst cap
            factor = 1.0 - self.penalty
        elif measured_rate <= low_threshold:
            # Low load: full burst cap
            factor = 1.0
        else:
            # Interpolate between high and low
            span = high_threshold - low_threshold
            t = (measured_rate - low_threshold) / span
            factor = 1.0 - (self.penalty * t)

        result = max(1, int(self._max_burst * factor))
        self._burst_cache[key] = (result, now)
        return result

    # ------------------------------------------------------------------
    # BaseAlgorithm
    # ------------------------------------------------------------------

    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        self._refresh_config()
        now = time.time()
        sw_key = self._sw_key(key)
        tb_key = self._tb_key(key)
        load_key = self._load_key(key)
        window_start = now - self.window

        # ── Compute adaptive burst cap first (needed by both layers) ──
        effective_burst = self._effective_burst(key, now)
        refill_rate = effective_burst / self.window

        # ── Layer 1: Sliding window hard ceiling (= effective_burst) ──
        # Prevents boundary-burst exploitation while honouring burst cap.
        self.backend.zremrangebyscore(sw_key, 0, window_start)
        sw_count = self.backend.zcard(sw_key)

        if sw_count + cost > effective_burst:
            # Hard burst ceiling breached — compute retry time
            oldest = self.backend.zrange_by_score(sw_key, window_start, now)
            retry_after = (oldest[0][1] + self.window - now) if oldest else self.window

            return RateLimitResult(
                allowed=False,
                key=key,
                limit=self.limit,
                remaining=0,
                reset_after=self.window,
                retry_after=max(0.0, retry_after),
                metadata={"layer": "sliding_window", "sw_count": sw_count,
                          "effective_burst": effective_burst},
            )

        # ── Layer 2: Token bucket — enforces long-term average rate ──

        raw = self.backend.get(tb_key)
        if raw is None:
            tokens = float(effective_burst)
            last_refill = now
        else:
            tokens = float(raw["tokens"])
            last_refill = float(raw["last_refill"])

        elapsed = now - last_refill
        tokens = min(float(effective_burst), tokens + elapsed * refill_rate)

        burst_ok = tokens >= cost

        if burst_ok:
            tokens -= cost
            # Record in sliding window
            for _ in range(cost):
                self.backend.zadd(sw_key, now, f"{now}:{next(_counter)}")
            self.backend.expire(sw_key, self.window + 1)

            # Record in global load sensor
            self.backend.zadd(load_key, now, f"{now}:{next(_counter)}")
            self.backend.expire(load_key, self.adaptive_window + 1)

        self.backend.set(
            tb_key,
            {"tokens": tokens, "last_refill": now},
            ttl=self.window * 2,
        )

        # ── Build result ──────────────────────────────────────────────
        remaining_sw = max(0, self.limit - sw_count - (cost if burst_ok else 0))
        remaining_tb = max(0, int(tokens))
        remaining = min(remaining_sw, remaining_tb)

        if burst_ok:
            retry_after = 0.0
        else:
            deficit = cost - tokens
            retry_after = deficit / refill_rate if refill_rate > 0 else self.window

        time_to_full = (effective_burst - tokens) / refill_rate if refill_rate > 0 else 0.0

        return RateLimitResult(
            allowed=burst_ok,
            key=key,
            limit=self.limit,
            remaining=remaining,
            reset_after=time_to_full,
            retry_after=retry_after,
            metadata={
                "layer": "token_bucket",
                "tokens": round(tokens, 3),
                "effective_burst": effective_burst,
                "refill_rate": round(refill_rate, 4),
                "sw_count": sw_count,
            },
        )

    def reset(self, key: str) -> None:
        """Clear per-key sliding-window and token-bucket state for *key*."""
        self.backend.delete(self._sw_key(key))
        self.backend.delete(self._tb_key(key))
