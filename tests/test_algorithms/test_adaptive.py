"""Tests for AdaptiveRateLimiter."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from smart_ratelimiter.algorithms.adaptive import AdaptiveRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend


@pytest.fixture
def limiter():
    return AdaptiveRateLimiter(
        MemoryBackend(),
        limit=10,
        window=10.0,
        burst_multiplier=2.0,
        adaptive_window=60.0,
    )


class TestAdaptiveBasics:
    def test_first_request_allowed(self, limiter):
        assert limiter.is_allowed("u").allowed is True

    def test_burst_allowed_when_quiet(self, limiter):
        # Under low load, burst cap = limit * multiplier = 20
        for _ in range(20):
            assert limiter.is_allowed("u").allowed is True

    def test_hard_ceiling_enforced(self, limiter):
        """Sliding window hard ceiling enforced at effective_burst (limit * multiplier = 20)."""
        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(20):
                assert limiter.is_allowed("u").allowed is True, "burst should be allowed"
            # Now the sliding window is full at effective_burst=20
            result = limiter.is_allowed("u")
            assert result.allowed is False
            assert result.metadata.get("layer") == "sliding_window"

    def test_result_fields_populated(self, limiter):
        result = limiter.is_allowed("u")
        assert result.key == "u"
        assert result.limit == 10
        assert result.reset_after >= 0
        assert result.retry_after == 0.0

    def test_independent_keys(self, limiter):
        for _ in range(20):
            limiter.is_allowed("A")
        # B has independent state
        assert limiter.is_allowed("B").allowed is True

    def test_reset_clears_all_state(self, limiter):
        base = 1_000_000.0
        with patch("time.time", return_value=base):
            for _ in range(20):
                limiter.is_allowed("u")
            assert limiter.is_allowed("u").allowed is False
        limiter.reset("u")
        with patch("time.time", return_value=base):
            assert limiter.is_allowed("u").allowed is True


class TestAdaptiveLoadSensing:
    def test_high_load_reduces_burst_cap(self):
        """Under sustained high traffic, effective burst should shrink."""
        backend = MemoryBackend()
        limiter = AdaptiveRateLimiter(
            backend,
            limit=10,
            window=10.0,
            burst_multiplier=3.0,
            adaptive_window=30.0,
            high_load_threshold=0.5,
            penalty=0.8,
        )

        base = 1_000_000.0
        # Simulate high traffic: flood the load sensor
        with patch("time.time", return_value=base):
            # Fill the adaptive window with many requests from other keys
            for i in range(50):
                limiter.is_allowed(f"heavy_user:{i}")

        # Now check effective burst for a new key under high load
        with patch("time.time", return_value=base + 1.0):
            burst_high = limiter._effective_burst("new_user", base + 1.0)

        # Under zero load, effective burst should be max
        fresh_backend = MemoryBackend()
        fresh_limiter = AdaptiveRateLimiter(
            fresh_backend, limit=10, window=10.0, burst_multiplier=3.0
        )
        burst_low = fresh_limiter._effective_burst("new_user", base + 1.0)

        assert burst_high < burst_low

    def test_low_load_grants_full_burst(self):
        backend = MemoryBackend()
        limiter = AdaptiveRateLimiter(
            backend,
            limit=10,
            window=10.0,
            burst_multiplier=2.0,
            adaptive_window=60.0,
        )
        burst = limiter._effective_burst("u", 1_000_000.0)
        # With no load, should get full burst cap
        assert burst == 20  # limit * multiplier

    def test_metadata_layer_reported(self, limiter):
        result = limiter.is_allowed("u")
        assert "layer" in result.metadata

    def test_metadata_tokens_reported(self, limiter):
        result = limiter.is_allowed("u")
        assert "tokens" in result.metadata
        assert "effective_burst" in result.metadata


class TestAdaptiveSlidingWindowGuard:
    def test_sliding_window_prevents_boundary_burst(self):
        """Hard ceiling prevents exploiting window boundaries."""
        backend = MemoryBackend()
        limiter = AdaptiveRateLimiter(
            backend, limit=5, window=10.0, burst_multiplier=1.0
        )

        base = 1_000_000.0
        with patch("time.time", return_value=base + 9.9):
            for _ in range(5):
                limiter.is_allowed("u")

        # Right after window rolls over — should not allow another 5 immediately
        # because the sliding window tracks the last 10s, not a fixed bucket
        with patch("time.time", return_value=base + 10.1):
            # Most of the previous 5 are still in the window (only 0.2s elapsed)
            results = [limiter.is_allowed("u") for _ in range(5)]
            allowed_count = sum(r.allowed for r in results)
            # Should not get all 5 through; sliding window prevents boundary burst
            assert allowed_count < 5
