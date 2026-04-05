"""Tests for TokenBucketRateLimiter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from ratelimiter.algorithms.token_bucket import TokenBucketRateLimiter
from ratelimiter.backends.memory import MemoryBackend


@pytest.fixture
def limiter():
    # 10 tokens capacity, refills fully in 10s → 1 token/s
    return TokenBucketRateLimiter(MemoryBackend(), limit=10, window=10.0)


class TestTokenBucketBasics:
    def test_first_request_allowed(self, limiter):
        assert limiter.is_allowed("u").allowed is True

    def test_burst_up_to_capacity(self, limiter):
        for _ in range(10):
            assert limiter.is_allowed("u").allowed is True

    def test_exceeding_capacity_rejected(self, limiter):
        for _ in range(10):
            limiter.is_allowed("u")
        assert limiter.is_allowed("u").allowed is False

    def test_remaining_tracks_tokens(self, limiter):
        r1 = limiter.is_allowed("u")
        r2 = limiter.is_allowed("u")
        assert r1.remaining > r2.remaining

    def test_retry_after_reflects_refill_time(self, limiter):
        for _ in range(10):
            limiter.is_allowed("u")
        result = limiter.is_allowed("u")
        assert result.retry_after > 0
        assert result.retry_after <= 10.0  # at most one full refill cycle

    def test_reset_refills_bucket(self, limiter):
        for _ in range(10):
            limiter.is_allowed("u")
        limiter.reset("u")
        assert limiter.is_allowed("u").allowed is True

    def test_independent_keys(self, limiter):
        for _ in range(10):
            limiter.is_allowed("A")
        assert limiter.is_allowed("B").allowed is True


class TestTokenBucketRefill:
    def test_tokens_refill_over_time(self):
        backend = MemoryBackend()
        limiter = TokenBucketRateLimiter(backend, limit=10, window=10.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(10):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False

        # 5 seconds later → 5 tokens refilled (1 token/s)
        with patch("time.time", return_value=base + 5.0):
            for _ in range(5):
                assert limiter.is_allowed("u").allowed is True
            assert limiter.is_allowed("u").allowed is False

    def test_does_not_exceed_capacity_on_refill(self):
        backend = MemoryBackend()
        limiter = TokenBucketRateLimiter(backend, limit=5, window=5.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            limiter.is_allowed("u")  # consume 1 token

        # Advance way beyond full refill time
        with patch("time.time", return_value=base + 1000.0):
            result = limiter.is_allowed("u")
            # Should have exactly limit tokens available (not over-filled)
            assert result.remaining <= 5


class TestTokenBucketCustomRefillRate:
    def test_custom_refill_rate(self):
        backend = MemoryBackend()
        # Capacity 100, but refills at 10 tokens/s
        limiter = TokenBucketRateLimiter(
            backend, limit=100, window=60.0, refill_rate=10.0
        )

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(100):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False

        # 1 second → 10 tokens refilled
        with patch("time.time", return_value=base + 1.0):
            for _ in range(10):
                assert limiter.is_allowed("u").allowed is True
            assert limiter.is_allowed("u").allowed is False


class TestTokenBucketCost:
    def test_cost_deducts_multiple_tokens(self):
        backend = MemoryBackend()
        limiter = TokenBucketRateLimiter(backend, limit=10, window=10.0)
        assert limiter.is_allowed("u", cost=10).allowed is True
        assert limiter.is_allowed("u", cost=1).allowed is False

    def test_cost_greater_than_capacity_rejected(self):
        backend = MemoryBackend()
        limiter = TokenBucketRateLimiter(backend, limit=5, window=10.0)
        assert limiter.is_allowed("u", cost=6).allowed is False
