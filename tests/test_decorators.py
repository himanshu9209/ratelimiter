"""Tests for decorators and context managers."""

from __future__ import annotations

import pytest

from ratelimiter.algorithms.fixed_window import FixedWindowRateLimiter
from ratelimiter.algorithms.token_bucket import TokenBucketRateLimiter
from ratelimiter.backends.memory import MemoryBackend
from ratelimiter.decorators import RateLimitContext, rate_limit
from ratelimiter.exceptions import RateLimitExceeded


@pytest.fixture
def small_limiter():
    return FixedWindowRateLimiter(MemoryBackend(), limit=3, window=60.0)


# ---------------------------------------------------------------------------
# @rate_limit decorator
# ---------------------------------------------------------------------------

class TestRateLimitDecorator:
    def test_decorated_function_called(self, small_limiter):
        @rate_limit(small_limiter)
        def work():
            return "done"

        assert work() == "done"

    def test_raises_on_limit_exceeded(self, small_limiter):
        @rate_limit(small_limiter)
        def work():
            return "ok"

        for _ in range(3):
            work()

        with pytest.raises(RateLimitExceeded):
            work()

    def test_returns_none_when_raise_disabled(self, small_limiter):
        @rate_limit(small_limiter, raise_on_limit=False)
        def work():
            return "ok"

        for _ in range(3):
            work()

        assert work() is None

    def test_static_key_used(self):
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=1, window=60.0)

        @rate_limit(limiter, key="shared")
        def fn_a():
            pass

        @rate_limit(limiter, key="shared")
        def fn_b():
            pass

        fn_a()  # consumes the 1 allowed request on key "shared"
        with pytest.raises(RateLimitExceeded):
            fn_b()  # same key → rejected

    def test_key_func_extracts_per_caller_key(self):
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=2, window=60.0)

        @rate_limit(limiter, key_func=lambda uid, **kw: f"user:{uid}")
        def fetch(uid: str):
            return uid

        fetch("alice")
        fetch("alice")
        with pytest.raises(RateLimitExceeded):
            fetch("alice")

        # Bob has his own counter
        assert fetch("bob") == "bob"

    def test_cost_parameter(self):
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=6, window=60.0)

        @rate_limit(limiter, cost=3)
        def expensive():
            pass

        expensive()  # consumes 3 tokens → total 3, allowed
        expensive()  # consumes 3 more → total 6, allowed (at limit)

        with pytest.raises(RateLimitExceeded):
            expensive()  # total 9 > 6 → rejected

    def test_limiter_attached_to_wrapper(self, small_limiter):
        @rate_limit(small_limiter)
        def fn():
            pass

        assert fn.limiter is small_limiter

    def test_functools_wraps_preserves_metadata(self, small_limiter):
        @rate_limit(small_limiter)
        def my_function():
            """My docstring."""

        assert my_function.__name__ == "my_function"
        assert my_function.__doc__ == "My docstring."

    def test_key_and_key_func_mutually_exclusive(self, small_limiter):
        with pytest.raises(ValueError):
            @rate_limit(
                small_limiter,
                key="static",
                key_func=lambda: "dynamic",
            )
            def fn():
                pass

    def test_exception_contains_retry_after(self, small_limiter):
        @rate_limit(small_limiter)
        def fn():
            pass

        for _ in range(3):
            fn()

        with pytest.raises(RateLimitExceeded) as exc_info:
            fn()

        assert exc_info.value.retry_after > 0

    def test_exception_contains_key(self, small_limiter):
        @rate_limit(small_limiter, key="mykey")
        def fn():
            pass

        for _ in range(3):
            fn()

        with pytest.raises(RateLimitExceeded) as exc_info:
            fn()

        assert exc_info.value.key == "mykey"


# ---------------------------------------------------------------------------
# RateLimitContext context manager
# ---------------------------------------------------------------------------

class TestRateLimitContext:
    def test_context_allows_request(self, small_limiter):
        with RateLimitContext(small_limiter, key="u"):
            pass  # should not raise

    def test_context_raises_when_exceeded(self, small_limiter):
        for _ in range(3):
            with RateLimitContext(small_limiter, key="u"):
                pass

        with pytest.raises(RateLimitExceeded):
            with RateLimitContext(small_limiter, key="u"):
                pass

    def test_result_accessible_after_enter(self, small_limiter):
        ctx = RateLimitContext(small_limiter, key="u")
        with ctx:
            assert ctx.result is not None
            assert ctx.result.allowed is True

    def test_exception_in_body_does_not_double_count(self):
        """Exceptions raised inside the block must not trigger extra charges."""
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=2, window=60.0)

        try:
            with RateLimitContext(limiter, key="u"):
                raise ValueError("oops")
        except ValueError:
            pass

        # Should have consumed exactly 1 of 2 allowed
        with RateLimitContext(limiter, key="u"):
            pass  # still within limit

    def test_cost_parameter(self):
        backend = MemoryBackend()
        limiter = FixedWindowRateLimiter(backend, limit=5, window=60.0)

        with RateLimitContext(limiter, key="u", cost=5):
            pass  # consumes all 5

        with pytest.raises(RateLimitExceeded):
            with RateLimitContext(limiter, key="u", cost=1):
                pass


# ---------------------------------------------------------------------------
# RateLimitExceeded exception
# ---------------------------------------------------------------------------

class TestRateLimitExceededException:
    def test_str_representation(self):
        exc = RateLimitExceeded(key="u", limit=10, window=60.0, retry_after=5.0)
        s = str(exc)
        assert "u" in s
        assert "10" in s
        assert "5.00" in s

    def test_attributes(self):
        exc = RateLimitExceeded(key="k", limit=5, window=30.0, retry_after=3.7)
        assert exc.key == "k"
        assert exc.limit == 5
        assert exc.window == 30.0
        assert exc.retry_after == 3.7
