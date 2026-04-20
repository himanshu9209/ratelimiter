"""Tests for FixedWindowRateLimiter."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from smart_ratelimiter.algorithms.fixed_window import FixedWindowRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend


@pytest.fixture
def limiter():
    return FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60.0)


class TestFixedWindowBasics:
    def test_first_request_allowed(self, limiter):
        result = limiter.is_allowed("user:1")
        assert result.allowed is True

    def test_within_limit_all_allowed(self, limiter):
        for _ in range(5):
            assert limiter.is_allowed("user:1").allowed is True

    def test_exceeding_limit_rejected(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user:1")
        result = limiter.is_allowed("user:1")
        assert result.allowed is False

    def test_different_keys_are_independent(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user:A")
        # user:B has its own counter
        assert limiter.is_allowed("user:B").allowed is True

    def test_remaining_decrements(self, limiter):
        r1 = limiter.is_allowed("user:1")
        r2 = limiter.is_allowed("user:1")
        assert r1.remaining > r2.remaining

    def test_remaining_is_zero_when_exhausted(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user:1")
        result = limiter.is_allowed("user:1")
        assert result.remaining == 0

    def test_result_fields_populated(self, limiter):
        result = limiter.is_allowed("user:1")
        assert result.key == "user:1"
        assert result.limit == 5
        assert result.reset_after > 0
        assert result.retry_after == 0.0

    def test_retry_after_set_when_rejected(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user:1")
        result = limiter.is_allowed("user:1")
        assert result.retry_after > 0

    def test_headers_keys_present(self, limiter):
        result = limiter.is_allowed("user:1")
        assert "X-RateLimit-Limit" in result.headers
        assert "X-RateLimit-Remaining" in result.headers
        assert "X-RateLimit-Reset" in result.headers

    def test_headers_retry_after_on_rejection(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user:1")
        result = limiter.is_allowed("user:1")
        assert "Retry-After" in result.headers

    def test_reset_clears_counter(self, limiter):
        for _ in range(5):
            limiter.is_allowed("user:1")
        assert limiter.is_allowed("user:1").allowed is False
        limiter.reset("user:1")
        assert limiter.is_allowed("user:1").allowed is True


class TestFixedWindowWindow:
    def test_new_window_resets_counter(self):
        """Simulate crossing a window boundary by mocking time."""
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=3, window=10.0)

        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(3):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False

        # Advance past the window boundary
        with patch("time.time", return_value=base + 10.0):
            assert limiter.is_allowed("u").allowed is True

    def test_cost_parameter(self):
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=10, window=60.0)
        result = limiter.is_allowed("u", cost=10)
        assert result.allowed is True
        # One more should be rejected
        result = limiter.is_allowed("u", cost=1)
        assert result.allowed is False

    def test_cost_exceeds_limit_rejected(self):
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=5, window=60.0)
        result = limiter.is_allowed("u", cost=6)
        assert result.allowed is False


class TestFixedWindowValidation:
    def test_zero_limit_raises(self):
        with pytest.raises(ValueError):
            FixedWindowRateLimiter(MemoryBackend(), limit=0, window=60)

    def test_negative_window_raises(self):
        with pytest.raises(ValueError):
            FixedWindowRateLimiter(MemoryBackend(), limit=10, window=-1)
