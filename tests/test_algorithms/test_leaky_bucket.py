"""Tests for LeakyBucketRateLimiter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from smart_ratelimiter.algorithms.leaky_bucket import LeakyBucketRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend


@pytest.fixture
def limiter():
    # capacity=5, drains fully in 5s → 1 req/s leak rate
    return LeakyBucketRateLimiter(MemoryBackend(), limit=5, window=5.0)


class TestLeakyBucketBasics:
    def test_first_request_allowed(self, limiter):
        assert limiter.is_allowed("u").allowed is True

    def test_up_to_capacity_allowed(self, limiter):
        for _ in range(5):
            assert limiter.is_allowed("u").allowed is True

    def test_over_capacity_rejected(self, limiter):
        for _ in range(5):
            limiter.is_allowed("u")
        assert limiter.is_allowed("u").allowed is False

    def test_remaining_decrements(self, limiter):
        r1 = limiter.is_allowed("u")
        r2 = limiter.is_allowed("u")
        assert r1.remaining > r2.remaining

    def test_retry_after_set_when_rejected(self, limiter):
        for _ in range(5):
            limiter.is_allowed("u")
        result = limiter.is_allowed("u")
        assert result.retry_after > 0

    def test_reset_empties_bucket(self, limiter):
        for _ in range(5):
            limiter.is_allowed("u")
        limiter.reset("u")
        assert limiter.is_allowed("u").allowed is True

    def test_independent_keys(self, limiter):
        for _ in range(5):
            limiter.is_allowed("A")
        assert limiter.is_allowed("B").allowed is True


class TestLeakyBucketDrain:
    def test_bucket_drains_over_time(self):
        backend = MemoryBackend()
        limiter = LeakyBucketRateLimiter(backend, limit=5, window=5.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(5):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False

        # 5 seconds → bucket fully drained (leak_rate=1 req/s)
        with patch("time.time", return_value=base + 5.0):
            assert limiter.is_allowed("u").allowed is True

    def test_partial_drain_frees_some_capacity(self):
        backend = MemoryBackend()
        limiter = LeakyBucketRateLimiter(backend, limit=4, window=4.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(4):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False

        # 2 seconds → 2 slots drained
        with patch("time.time", return_value=base + 2.0):
            for _ in range(2):
                assert limiter.is_allowed("u").allowed is True
            assert limiter.is_allowed("u").allowed is False


class TestLeakyBucketCost:
    def test_cost_fills_multiple_slots(self):
        backend = MemoryBackend()
        limiter = LeakyBucketRateLimiter(backend, limit=10, window=10.0)
        assert limiter.is_allowed("u", cost=10).allowed is True
        assert limiter.is_allowed("u", cost=1).allowed is False

    def test_cost_exceeds_capacity_rejected(self):
        backend = MemoryBackend()
        limiter = LeakyBucketRateLimiter(backend, limit=5, window=5.0)
        assert limiter.is_allowed("u", cost=6).allowed is False
