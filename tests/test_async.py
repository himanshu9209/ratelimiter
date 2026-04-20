"""Tests for async wrappers."""

from __future__ import annotations

import asyncio

import pytest

from smart_ratelimiter import (
    AdaptiveRateLimiter,
    FixedWindowRateLimiter,
    MemoryBackend,
    RateLimitExceeded,
)
from smart_ratelimiter.algorithms.async_base import AsyncRateLimiter, async_rate_limit


@pytest.fixture
def sync_limiter():
    return FixedWindowRateLimiter(MemoryBackend(), limit=3, window=60.0)


@pytest.fixture
def async_limiter(sync_limiter):
    return AsyncRateLimiter(sync_limiter)


class TestAsyncRateLimiter:
    @pytest.mark.asyncio
    async def test_first_request_allowed(self, async_limiter):
        result = await async_limiter.is_allowed("u")
        assert result.allowed is True

    @pytest.mark.asyncio
    async def test_within_limit_all_allowed(self, async_limiter):
        for _ in range(3):
            result = await async_limiter.is_allowed("u")
            assert result.allowed is True

    @pytest.mark.asyncio
    async def test_over_limit_rejected(self, async_limiter):
        for _ in range(3):
            await async_limiter.is_allowed("u")
        result = await async_limiter.is_allowed("u")
        assert result.allowed is False
        assert result.retry_after > 0

    @pytest.mark.asyncio
    async def test_reset_clears_state(self, async_limiter):
        for _ in range(3):
            await async_limiter.is_allowed("u")
        assert (await async_limiter.is_allowed("u")).allowed is False
        await async_limiter.reset("u")
        assert (await async_limiter.is_allowed("u")).allowed is True

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, async_limiter):
        """Concurrent coroutines must each get a correct result."""
        results = await asyncio.gather(
            *[async_limiter.is_allowed("u") for _ in range(6)]
        )
        allowed = [r for r in results if r.allowed]
        rejected = [r for r in results if not r.allowed]
        assert len(allowed) == 3
        assert len(rejected) == 3

    @pytest.mark.asyncio
    async def test_proxy_properties(self, async_limiter, sync_limiter):
        assert async_limiter.limit == sync_limiter.limit
        assert async_limiter.window == sync_limiter.window

    @pytest.mark.asyncio
    async def test_adaptive_limiter_async(self):
        sync = AdaptiveRateLimiter(MemoryBackend(), limit=5, window=10, burst_multiplier=2)
        limiter = AsyncRateLimiter(sync)
        for _ in range(10):
            result = await limiter.is_allowed("u")
            assert result.allowed is True
        assert (await limiter.is_allowed("u")).allowed is False


class TestAsyncRateLimitDecorator:
    @pytest.mark.asyncio
    async def test_decorated_async_function_called(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter)
        async def work():
            return "done"

        assert await work() == "done"

    @pytest.mark.asyncio
    async def test_raises_on_limit_exceeded(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=2, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter, key="t")
        async def fn():
            return "ok"

        await fn()
        await fn()
        with pytest.raises(RateLimitExceeded) as exc_info:
            await fn()
        assert exc_info.value.key == "t"
        assert exc_info.value.retry_after > 0

    @pytest.mark.asyncio
    async def test_returns_none_when_raise_disabled(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=1, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter, raise_on_limit=False)
        async def fn():
            return "ok"

        assert await fn() == "ok"
        assert await fn() is None

    @pytest.mark.asyncio
    async def test_key_func_per_caller(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=1, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter, key_func=lambda uid: f"user:{uid}")
        async def fetch(uid: str):
            return uid

        assert await fetch("alice") == "alice"
        with pytest.raises(RateLimitExceeded):
            await fetch("alice")
        # Bob has his own counter
        assert await fetch("bob") == "bob"

    @pytest.mark.asyncio
    async def test_cost_parameter(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter, cost=3)
        async def fn():
            pass

        await fn()  # 3 tokens
        with pytest.raises(RateLimitExceeded):
            await fn()  # needs 3 more; only 2 remain

    @pytest.mark.asyncio
    async def test_functools_wraps(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter)
        async def my_func():
            """My docstring."""

        assert my_func.__name__ == "my_func"
        assert my_func.__doc__ == "My docstring."

    @pytest.mark.asyncio
    async def test_limiter_attached_to_wrapper(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60)
        limiter = AsyncRateLimiter(sync)

        @async_rate_limit(limiter)
        async def fn():
            pass

        assert fn.limiter is limiter

    def test_key_and_key_func_mutually_exclusive(self):
        sync = FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60)
        limiter = AsyncRateLimiter(sync)
        with pytest.raises(ValueError):
            @async_rate_limit(limiter, key="static", key_func=lambda: "dynamic")
            async def fn():
                pass
