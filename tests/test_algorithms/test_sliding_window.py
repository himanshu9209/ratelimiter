"""Tests for SlidingWindowRateLimiter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from smart_ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend


@pytest.fixture
def limiter():
    return SlidingWindowRateLimiter(MemoryBackend(), limit=5, window=10.0)


class TestSlidingWindowBasics:
    def test_first_request_allowed(self, limiter):
        assert limiter.is_allowed("u").allowed is True

    def test_within_limit_allowed(self, limiter):
        for _ in range(5):
            assert limiter.is_allowed("u").allowed is True

    def test_over_limit_rejected(self, limiter):
        for _ in range(5):
            limiter.is_allowed("u")
        assert limiter.is_allowed("u").allowed is False

    def test_independent_keys(self, limiter):
        for _ in range(5):
            limiter.is_allowed("A")
        assert limiter.is_allowed("B").allowed is True

    def test_retry_after_positive_when_rejected(self, limiter):
        for _ in range(5):
            limiter.is_allowed("u")
        result = limiter.is_allowed("u")
        assert result.retry_after > 0

    def test_reset_clears_state(self, limiter):
        for _ in range(5):
            limiter.is_allowed("u")
        limiter.reset("u")
        assert limiter.is_allowed("u").allowed is True


class TestSlidingWindowExpiry:
    def test_old_requests_fall_out_of_window(self):
        """Requests older than the window should no longer count."""
        backend = MemoryBackend()
        limiter = SlidingWindowRateLimiter(backend, limit=3, window=5.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(3):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False

        # Advance so all previous requests are outside the 5-second window
        with patch("time.time", return_value=base + 6.0):
            assert limiter.is_allowed("u").allowed is True

    def test_partial_expiry_allows_more(self):
        """Partially expired window should free up slots."""
        backend = MemoryBackend()
        limiter = SlidingWindowRateLimiter(backend, limit=3, window=10.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            limiter.is_allowed("u")  # recorded at t=0

        with patch("time.time", return_value=base + 5.0):
            limiter.is_allowed("u")  # at t=5
            limiter.is_allowed("u")  # at t=5 — limit hit

        # At t=11, the first entry (t=0) falls out; one slot free
        with patch("time.time", return_value=base + 11.0):
            result = limiter.is_allowed("u")
            assert result.allowed is True


class TestSlidingWindowCost:
    def test_cost_counts_multiple_slots(self):
        backend = MemoryBackend()
        limiter = SlidingWindowRateLimiter(backend, limit=5, window=10.0)
        result = limiter.is_allowed("u", cost=5)
        assert result.allowed is True
        assert limiter.is_allowed("u", cost=1).allowed is False

    def test_cost_exceeds_limit_rejected(self):
        backend = MemoryBackend()
        limiter = SlidingWindowRateLimiter(backend, limit=3, window=10.0)
        assert limiter.is_allowed("u", cost=4).allowed is False
