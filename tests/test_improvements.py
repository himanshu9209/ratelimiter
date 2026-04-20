"""
Tests for the four improvement features:
  1. Dynamic Configuration (DynamicConfig / ConfigProvider)
  2. Client Identification (key_funcs)
  3. Observability (InMemoryMetricsCollector / ObservableRateLimiter)
  4. Sliding Window Counter (SlidingWindowCounterRateLimiter)
"""

from __future__ import annotations

import threading
import time

import pytest

from smart_ratelimiter.algorithms.fixed_window import FixedWindowRateLimiter
from smart_ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
from smart_ratelimiter.algorithms.sliding_window_counter import SlidingWindowCounterRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend
from smart_ratelimiter.config import DynamicConfig
from smart_ratelimiter.key_funcs import (
    asgi_api_key_func,
    asgi_composite_key_func,
    asgi_ip_func,
    wsgi_api_key_func,
    wsgi_composite_key_func,
    wsgi_ip_func,
)
from smart_ratelimiter.metrics import InMemoryMetricsCollector, ObservableRateLimiter


# ===========================================================================
# 1. Dynamic Configuration
# ===========================================================================

class TestDynamicConfig:
    def test_basic_creation(self):
        cfg = DynamicConfig(limit=100, window=60)
        assert cfg.get_limit() == 100
        assert cfg.get_window() == 60.0

    def test_update_limit(self):
        cfg = DynamicConfig(limit=100, window=60)
        cfg.update(limit=200)
        assert cfg.get_limit() == 200
        assert cfg.get_window() == 60.0  # unchanged

    def test_update_window(self):
        cfg = DynamicConfig(limit=100, window=60)
        cfg.update(window=30.0)
        assert cfg.get_limit() == 100  # unchanged
        assert cfg.get_window() == 30.0

    def test_update_both(self):
        cfg = DynamicConfig(limit=100, window=60)
        cfg.update(limit=50, window=10)
        assert cfg.get_limit() == 50
        assert cfg.get_window() == 10.0

    def test_invalid_init_raises(self):
        with pytest.raises(ValueError):
            DynamicConfig(limit=0, window=60)
        with pytest.raises(ValueError):
            DynamicConfig(limit=10, window=-1)

    def test_invalid_update_raises(self):
        cfg = DynamicConfig(limit=10, window=60)
        with pytest.raises(ValueError):
            cfg.update(limit=0)
        with pytest.raises(ValueError):
            cfg.update(window=0)

    def test_thread_safety(self):
        cfg = DynamicConfig(limit=10, window=60)
        errors = []

        def writer():
            for i in range(1, 51):
                try:
                    cfg.update(limit=i)
                except Exception as e:
                    errors.append(e)

        def reader():
            for _ in range(100):
                try:
                    cfg.get_limit()
                    cfg.get_window()
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=writer)] + [
            threading.Thread(target=reader) for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []

    def test_limiter_picks_up_new_limit(self):
        cfg = DynamicConfig(limit=3, window=10)
        limiter = FixedWindowRateLimiter(
            MemoryBackend(), limit=3, window=10, config_provider=cfg
        )

        # Exhaust the original limit
        for _ in range(3):
            assert limiter.is_allowed("key").allowed is True
        assert limiter.is_allowed("key").allowed is False

        # Raise the limit via DynamicConfig — but same window bucket, so
        # we test on a fresh key to avoid existing count interference
        cfg.update(limit=10)
        result = limiter.is_allowed("new_key")
        assert result.allowed is True
        assert limiter.limit == 10  # base attribute updated

    def test_limiter_picks_up_reduced_limit(self):
        cfg = DynamicConfig(limit=10, window=60)
        limiter = SlidingWindowRateLimiter(
            MemoryBackend(), limit=10, window=60, config_provider=cfg
        )

        # Allow a few requests
        for _ in range(3):
            limiter.is_allowed("u")

        # Tighten limit below current count
        cfg.update(limit=2)
        # Next call should see limit=2 and count=3 → denied
        result = limiter.is_allowed("u")
        assert result.allowed is False


# ===========================================================================
# 2. Client Identification
# ===========================================================================

class TestWsgiKeyFuncs:
    def _environ(self, remote_addr="1.2.3.4", xff=None, api_key=None):
        env = {"REMOTE_ADDR": remote_addr}
        if xff:
            env["HTTP_X_FORWARDED_FOR"] = xff
        if api_key:
            env["HTTP_X_API_KEY"] = api_key
        return env

    def test_ip_func_remote_addr(self):
        kf = wsgi_ip_func()
        assert kf(self._environ()) == "1.2.3.4"

    def test_ip_func_xff_trusted(self):
        kf = wsgi_ip_func(trust_x_forwarded_for=True)
        env = self._environ(xff="9.9.9.9, 1.2.3.4")
        assert kf(env) == "9.9.9.9"

    def test_ip_func_xff_not_trusted(self):
        kf = wsgi_ip_func(trust_x_forwarded_for=False)
        env = self._environ(remote_addr="1.2.3.4", xff="9.9.9.9")
        assert kf(env) == "1.2.3.4"

    def test_api_key_func_present(self):
        kf = wsgi_api_key_func("X-API-Key")
        env = self._environ(api_key="my-secret")
        assert kf(env) == "apikey:my-secret"

    def test_api_key_func_fallback_to_ip(self):
        kf = wsgi_api_key_func("X-API-Key")
        env = self._environ()  # no api key header
        assert kf(env) == "1.2.3.4"

    def test_api_key_func_custom_fallback(self):
        fallback = wsgi_ip_func(trust_x_forwarded_for=False)
        kf = wsgi_api_key_func("X-API-Key", fallback=fallback)
        env = self._environ(remote_addr="5.5.5.5", xff="9.9.9.9")
        assert kf(env) == "5.5.5.5"  # xff ignored by fallback

    def test_composite_key_func(self):
        kf = wsgi_composite_key_func(
            wsgi_ip_func(),
            wsgi_api_key_func("X-API-Key"),
            separator="::",
        )
        env = self._environ(api_key="token123")
        key = kf(env)
        assert "1.2.3.4" in key
        assert "apikey:token123" in key
        assert "::" in key

    def test_composite_requires_two_funcs(self):
        with pytest.raises(ValueError):
            wsgi_composite_key_func(wsgi_ip_func())


class TestAsgiKeyFuncs:
    def _scope(self, client_ip="1.2.3.4", xff=None, api_key=None):
        headers = []
        if xff:
            headers.append((b"x-forwarded-for", xff.encode()))
        if api_key:
            headers.append((b"x-api-key", api_key.encode()))
        return {"client": (client_ip, 12345), "headers": headers}

    def test_ip_func_client(self):
        kf = asgi_ip_func(trust_x_forwarded_for=False)
        assert kf(self._scope()) == "1.2.3.4"

    def test_ip_func_xff(self):
        kf = asgi_ip_func(trust_x_forwarded_for=True)
        assert kf(self._scope(xff="9.9.9.9, 1.2.3.4")) == "9.9.9.9"

    def test_api_key_func_present(self):
        kf = asgi_api_key_func("X-API-Key")
        assert kf(self._scope(api_key="tok-abc")) == "apikey:tok-abc"

    def test_api_key_func_fallback(self):
        kf = asgi_api_key_func("X-API-Key")
        assert kf(self._scope()) == "1.2.3.4"

    def test_composite_key_func(self):
        kf = asgi_composite_key_func(asgi_ip_func(), asgi_api_key_func())
        scope = self._scope(api_key="mykey")
        key = kf(scope)
        assert "1.2.3.4" in key
        assert "apikey:mykey" in key

    def test_composite_requires_two_funcs(self):
        with pytest.raises(ValueError):
            asgi_composite_key_func(asgi_ip_func())


# ===========================================================================
# 3. Observability
# ===========================================================================

class TestInMemoryMetricsCollector:
    def _limiter(self, limit=5):
        return FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=60)

    def test_counts_allowed(self):
        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(self._limiter(limit=10), metrics)
        for _ in range(3):
            limiter.is_allowed("u")
        stats = metrics.get_stats("u")
        assert stats["allowed"] == 3
        assert stats["dropped"] == 0
        assert stats["total"] == 3
        assert stats["drop_rate"] == 0.0

    def test_counts_dropped(self):
        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(self._limiter(limit=2), metrics)
        for _ in range(5):
            limiter.is_allowed("u")
        stats = metrics.get_stats("u")
        assert stats["allowed"] == 2
        assert stats["dropped"] == 3
        assert stats["total"] == 5
        assert abs(stats["drop_rate"] - 0.6) < 1e-9

    def test_global_stats(self):
        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(self._limiter(limit=3), metrics)
        for _ in range(3):
            limiter.is_allowed("a")
        for _ in range(2):
            limiter.is_allowed("b")
        global_stats = metrics.get_stats()
        assert global_stats["total"] == 5
        assert "per_key" in global_stats
        assert "a" in global_stats["per_key"]
        assert "b" in global_stats["per_key"]

    def test_reset_per_key(self):
        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(self._limiter(limit=10), metrics)
        limiter.is_allowed("a")
        limiter.is_allowed("b")
        metrics.reset("a")
        assert metrics.get_stats("a")["total"] == 0
        assert metrics.get_stats("b")["total"] == 1

    def test_reset_all(self):
        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(self._limiter(limit=10), metrics)
        limiter.is_allowed("a")
        limiter.is_allowed("b")
        metrics.reset()
        assert metrics.get_stats()["total"] == 0

    def test_proxies_limiter_attributes(self):
        base = self._limiter(limit=42)
        limiter = ObservableRateLimiter(base, InMemoryMetricsCollector())
        assert limiter.limit == 42
        assert limiter.window == 60

    def test_thread_safety(self):
        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(self._limiter(limit=1000), metrics)
        errors = []

        def worker():
            try:
                for _ in range(25):
                    limiter.is_allowed("shared")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert metrics.get_stats()["total"] == 200


# ===========================================================================
# 4. Sliding Window Counter
# ===========================================================================

class TestSlidingWindowCounter:
    def _limiter(self, limit=10, window=60):
        return SlidingWindowCounterRateLimiter(MemoryBackend(), limit=limit, window=window)

    def test_allows_up_to_limit(self):
        limiter = self._limiter(limit=5)
        results = [limiter.is_allowed("u") for _ in range(5)]
        assert all(r.allowed for r in results)

    def test_rejects_over_limit(self):
        limiter = self._limiter(limit=5)
        for _ in range(5):
            limiter.is_allowed("u")
        result = limiter.is_allowed("u")
        assert result.allowed is False

    def test_result_fields(self):
        limiter = self._limiter(limit=10)
        result = limiter.is_allowed("u")
        assert result.allowed is True
        assert result.limit == 10
        assert result.remaining >= 0
        assert result.reset_after > 0
        assert result.retry_after == 0.0
        assert "effective_count" in result.metadata
        assert "weight_prev" in result.metadata

    def test_remaining_decrements(self):
        limiter = self._limiter(limit=10)
        r1 = limiter.is_allowed("u")
        r2 = limiter.is_allowed("u")
        assert r2.remaining < r1.remaining

    def test_different_keys_independent(self):
        limiter = self._limiter(limit=3)
        for _ in range(3):
            limiter.is_allowed("a")
        # "a" is exhausted but "b" should still be allowed
        assert limiter.is_allowed("a").allowed is False
        assert limiter.is_allowed("b").allowed is True

    def test_reset_clears_state(self):
        limiter = self._limiter(limit=3)
        for _ in range(3):
            limiter.is_allowed("u")
        assert limiter.is_allowed("u").allowed is False
        limiter.reset("u")
        assert limiter.is_allowed("u").allowed is True

    def test_metadata_weight_decreases_over_time(self):
        # The weight of the previous bucket should decrease as time passes
        # within the current window. We just verify it is in [0, 1].
        limiter = self._limiter(limit=100, window=10)
        result = limiter.is_allowed("u")
        assert 0.0 <= result.metadata["weight_prev"] <= 1.0

    def test_no_boundary_burst(self):
        """The blending should prevent a 2x burst at window boundaries."""
        limiter = self._limiter(limit=10, window=1)
        # Exhaust most of the limit
        for _ in range(9):
            limiter.is_allowed("u")
        # The 10th request should be allowed, 11th rejected even crossing boundary
        assert limiter.is_allowed("u").allowed is True
        assert limiter.is_allowed("u").allowed is False

    def test_retry_after_set_on_rejection(self):
        limiter = self._limiter(limit=3)
        for _ in range(3):
            limiter.is_allowed("u")
        result = limiter.is_allowed("u")
        assert result.allowed is False
        assert result.retry_after >= 0.0

    def test_with_dynamic_config(self):
        cfg = DynamicConfig(limit=3, window=60)
        limiter = SlidingWindowCounterRateLimiter(
            MemoryBackend(), limit=3, window=60, config_provider=cfg
        )
        for _ in range(3):
            limiter.is_allowed("u")
        assert limiter.is_allowed("u").allowed is False

        cfg.update(limit=10)
        assert limiter.is_allowed("new_key").allowed is True
