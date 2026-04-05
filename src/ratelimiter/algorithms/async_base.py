"""
Async wrappers for rate-limiting algorithms.

All core algorithms are synchronous because they operate on in-memory
data structures or use blocking I/O (SQLite, Redis with sync client).
These wrappers run them in a thread-pool executor so they can be awaited
inside async frameworks without blocking the event loop.

For Redis, prefer ``redis.asyncio`` and subclass ``AsyncBaseAlgorithm``
directly with a fully async backend if you need true non-blocking I/O.

Usage::

    from ratelimiter.algorithms.async_base import AsyncRateLimiter
    from ratelimiter.algorithms.adaptive import AdaptiveRateLimiter
    from ratelimiter.backends.memory import MemoryBackend

    sync_limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60)
    limiter = AsyncRateLimiter(sync_limiter)

    result = await limiter.is_allowed("user:42")
"""

from __future__ import annotations

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Optional, TypeVar

from .base import BaseAlgorithm, RateLimitResult

F = TypeVar("F", bound=Callable[..., Any])

# Module-level executor — shared across all AsyncRateLimiter instances.
# Override with AsyncRateLimiter(limiter, executor=my_executor) if needed.
_DEFAULT_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ratelimiter")


class AsyncRateLimiter:
    """Async wrapper around any synchronous :class:`BaseAlgorithm`.

    Delegates ``is_allowed`` to a thread-pool executor so it never blocks
    the event loop.

    Args:
        limiter:  A configured synchronous rate-limiter instance.
        executor: Optional custom ``ThreadPoolExecutor``.  Defaults to a
                  shared 4-thread pool.

    Example::

        import asyncio
        from ratelimiter import AdaptiveRateLimiter, MemoryBackend
        from ratelimiter.algorithms.async_base import AsyncRateLimiter

        async def main():
            sync = AdaptiveRateLimiter(MemoryBackend(), limit=10, window=1)
            limiter = AsyncRateLimiter(sync)

            result = await limiter.is_allowed("user:42")
            print(result.allowed, result.remaining)

        asyncio.run(main())
    """

    def __init__(
        self,
        limiter: BaseAlgorithm,
        executor: Optional[ThreadPoolExecutor] = None,
    ) -> None:
        self._limiter = limiter
        self._executor = executor or _DEFAULT_EXECUTOR

    async def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        """Non-blocking check.  Runs the sync limiter in a thread pool."""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            self._executor,
            functools.partial(self._limiter.is_allowed, key, cost),
        )

    async def reset(self, key: str) -> None:
        """Non-blocking reset."""
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            self._executor,
            functools.partial(self._limiter.reset, key),
        )

    # Proxy read-only properties for convenience
    @property
    def limit(self) -> int:
        return self._limiter.limit

    @property
    def window(self) -> float:
        return self._limiter.window


def async_rate_limit(
    limiter: "AsyncRateLimiter",
    key: Optional[str] = None,
    key_func: Optional[Callable[..., str]] = None,
    cost: int = 1,
    raise_on_limit: bool = True,
) -> Callable[[F], F]:
    """Async version of :func:`ratelimiter.decorators.rate_limit`.

    Works with ``async def`` functions.

    Args:
        limiter:        An :class:`AsyncRateLimiter` instance.
        key:            Static rate-limit key (mutually exclusive with *key_func*).
        key_func:       Callable that maps ``(*args, **kwargs)`` → ``str``.
        cost:           Tokens consumed per call.
        raise_on_limit: Raise :class:`~ratelimiter.exceptions.RateLimitExceeded`
                        when ``True`` (default), else return ``None``.

    Example::

        from ratelimiter import AdaptiveRateLimiter, MemoryBackend
        from ratelimiter.algorithms.async_base import AsyncRateLimiter, async_rate_limit

        _sync = AdaptiveRateLimiter(MemoryBackend(), limit=5, window=1)
        _async = AsyncRateLimiter(_sync)

        @async_rate_limit(_async, key_func=lambda uid, **kw: f\"user:{uid}\")
        async def fetch_data(uid: str) -> dict:
            return {}
    """
    import functools

    from ratelimiter.exceptions import RateLimitExceeded

    if key is not None and key_func is not None:
        raise ValueError("Specify at most one of 'key' or 'key_func'.")

    def decorator(func: F) -> F:
        resolved_key = key or func.__qualname__

        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            effective_key = key_func(*args, **kwargs) if key_func else resolved_key
            result = await limiter.is_allowed(effective_key, cost)

            if not result.allowed:
                if raise_on_limit:
                    raise RateLimitExceeded(
                        key=effective_key,
                        limit=result.limit,
                        window=limiter.window,
                        retry_after=result.retry_after,
                    )
                return None

            return await func(*args, **kwargs)

        wrapper.limiter = limiter  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator
