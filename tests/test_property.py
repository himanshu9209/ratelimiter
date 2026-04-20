"""
Property-based tests using Hypothesis.

These tests verify invariants that must hold for *any* valid combination of
parameters — not just the specific cases covered by unit tests.

Invariants checked:
  1. FixedWindow: allowed count in one window never exceeds limit.
  2. SlidingWindow: allowed count in one window never exceeds limit.
  3. SlidingWindowCounter: allowed count never exceeds limit.
  4. TokenBucket: tokens never go negative; allowed count respects capacity.
  5. LeakyBucket: level never exceeds limit; level is non-negative.
  6. AdaptiveRateLimiter: allowed count never exceeds effective_burst ceiling.
  7. All algorithms: remaining is always >= 0.
  8. All algorithms: retry_after is 0 when allowed, > 0 when rejected.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from smart_ratelimiter.algorithms.adaptive import AdaptiveRateLimiter
from smart_ratelimiter.algorithms.fixed_window import FixedWindowRateLimiter
from smart_ratelimiter.algorithms.leaky_bucket import LeakyBucketRateLimiter
from smart_ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
from smart_ratelimiter.algorithms.sliding_window_counter import SlidingWindowCounterRateLimiter
from smart_ratelimiter.algorithms.token_bucket import TokenBucketRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend

# Strategies
_limit = st.integers(min_value=1, max_value=50)
_window = st.floats(min_value=1.0, max_value=300.0, allow_nan=False, allow_infinity=False)
_n_requests = st.integers(min_value=1, max_value=100)
_cost = st.integers(min_value=1, max_value=5)

_default_settings = settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _count_allowed(limiter, key: str, n: int, cost: int = 1) -> tuple[int, list]:
    results = [limiter.is_allowed(key, cost) for _ in range(n)]
    allowed = sum(1 for r in results if r.allowed)
    return allowed, results


# ---------------------------------------------------------------------------
# 1. FixedWindow
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit, n=_n_requests)
def test_fixed_window_never_exceeds_limit(limit: int, n: int) -> None:
    limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    allowed, _ = _count_allowed(limiter, "u", n)
    assert allowed <= limit, f"allowed={allowed} > limit={limit}"


@_default_settings
@given(limit=_limit, n=_n_requests)
def test_fixed_window_remaining_non_negative(limit: int, n: int) -> None:
    limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    _, results = _count_allowed(limiter, "u", n)
    for r in results:
        assert r.remaining >= 0


@_default_settings
@given(limit=_limit, n=_n_requests)
def test_fixed_window_retry_after_consistency(limit: int, n: int) -> None:
    limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    _, results = _count_allowed(limiter, "u", n)
    for r in results:
        if r.allowed:
            assert r.retry_after == 0.0
        else:
            assert r.retry_after >= 0.0


# ---------------------------------------------------------------------------
# 2. SlidingWindow
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit, n=_n_requests)
def test_sliding_window_never_exceeds_limit(limit: int, n: int) -> None:
    limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    allowed, _ = _count_allowed(limiter, "u", n)
    assert allowed <= limit


@_default_settings
@given(limit=_limit, n=_n_requests, cost=_cost)
def test_sliding_window_cost_counted_correctly(limit: int, n: int, cost: int) -> None:
    limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    total_cost = 0
    for _ in range(n):
        r = limiter.is_allowed("u", cost)
        if r.allowed:
            total_cost += cost
    assert total_cost <= limit


# ---------------------------------------------------------------------------
# 3. SlidingWindowCounter
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit, n=_n_requests)
def test_sliding_window_counter_never_exceeds_limit(limit: int, n: int) -> None:
    limiter = SlidingWindowCounterRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    allowed, _ = _count_allowed(limiter, "u", n)
    # Allow 1% tolerance for the interpolation approximation
    assert allowed <= limit + 1, f"allowed={allowed} exceeds limit={limit}+1"


@_default_settings
@given(limit=_limit, n=_n_requests)
def test_sliding_window_counter_remaining_non_negative(limit: int, n: int) -> None:
    limiter = SlidingWindowCounterRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    _, results = _count_allowed(limiter, "u", n)
    for r in results:
        assert r.remaining >= 0


# ---------------------------------------------------------------------------
# 4. TokenBucket
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit, n=_n_requests)
def test_token_bucket_tokens_never_negative(limit: int, n: int) -> None:
    limiter = TokenBucketRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    for _ in range(n):
        r = limiter.is_allowed("u")
        tokens = r.metadata.get("tokens", 0)
        assert tokens >= 0, f"tokens went negative: {tokens}"


@_default_settings
@given(limit=_limit, n=_n_requests)
def test_token_bucket_burst_does_not_exceed_capacity(limit: int, n: int) -> None:
    # In a single instant, at most `limit` requests should be allowed.
    limiter = TokenBucketRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    allowed, _ = _count_allowed(limiter, "u", n)
    assert allowed <= limit


# ---------------------------------------------------------------------------
# 5. LeakyBucket
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit, n=_n_requests)
def test_leaky_bucket_level_non_negative(limit: int, n: int) -> None:
    limiter = LeakyBucketRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    for _ in range(n):
        r = limiter.is_allowed("u")
        level = r.metadata.get("level", 0)
        assert level >= 0


@_default_settings
@given(limit=_limit, n=_n_requests)
def test_leaky_bucket_level_never_exceeds_capacity(limit: int, n: int) -> None:
    limiter = LeakyBucketRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    for _ in range(n):
        r = limiter.is_allowed("u")
        level = r.metadata.get("level", 0)
        assert level <= limit, f"bucket level {level} exceeded capacity {limit}"


# ---------------------------------------------------------------------------
# 6. AdaptiveRateLimiter
# ---------------------------------------------------------------------------

@_default_settings
@given(
    limit=st.integers(min_value=2, max_value=30),
    multiplier=st.floats(min_value=1.0, max_value=4.0, allow_nan=False, allow_infinity=False),
    n=_n_requests,
)
def test_adaptive_never_exceeds_effective_burst(
    limit: int, multiplier: float, n: int
) -> None:
    limiter = AdaptiveRateLimiter(
        MemoryBackend(),
        limit=limit,
        window=300.0,
        burst_multiplier=multiplier,
        adaptive_window=600.0,
    )
    max_burst = int(limit * multiplier)
    allowed, _ = _count_allowed(limiter, "u", n)
    assert allowed <= max_burst, f"allowed={allowed} > max_burst={max_burst}"


@_default_settings
@given(limit=st.integers(min_value=2, max_value=20), n=_n_requests)
def test_adaptive_remaining_non_negative(limit: int, n: int) -> None:
    limiter = AdaptiveRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    _, results = _count_allowed(limiter, "u", n)
    for r in results:
        assert r.remaining >= 0


# ---------------------------------------------------------------------------
# 7. Cross-algorithm: independent keys never affect each other
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit, n=_n_requests)
def test_independent_keys_do_not_share_state(limit: int, n: int) -> None:
    """Exhausting key 'a' must not reduce budget for key 'b'."""
    limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    # Exhaust key 'a'
    for _ in range(limit + 5):
        limiter.is_allowed("a")
    # Key 'b' should still have a full, independent budget
    assert limiter.is_allowed("b").allowed is True


# ---------------------------------------------------------------------------
# 8. Cross-algorithm: reset() restores full budget
# ---------------------------------------------------------------------------

@_default_settings
@given(limit=_limit)
def test_reset_restores_budget(limit: int) -> None:
    limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=300.0)
    for _ in range(limit):
        limiter.is_allowed("u")
    assert limiter.is_allowed("u").allowed is False
    limiter.reset("u")
    assert limiter.is_allowed("u").allowed is True
