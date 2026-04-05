"""
Decorator and context-manager interface for rate limiting.

The ``@rate_limit`` decorator is the highest-level API.  It wraps any
callable and raises :class:`~ratelimiter.exceptions.RateLimitExceeded`
when the limit is breached.

Key extraction
--------------
By default, the key is the name of the decorated function.  Supply a
``key_func`` to derive a per-caller key from the function's arguments::

    @rate_limit(limiter, key_func=lambda user_id, *a, **kw: f"user:{user_id}")
    def get_profile(user_id: int) -> dict:
        ...
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional, TypeVar

from .algorithms.base import BaseAlgorithm, RateLimitResult
from .exceptions import RateLimitExceeded

F = TypeVar("F", bound=Callable[..., Any])


def rate_limit(
    limiter: BaseAlgorithm,
    key: Optional[str] = None,
    key_func: Optional[Callable[..., str]] = None,
    cost: int = 1,
    raise_on_limit: bool = True,
) -> Callable[[F], F]:
    """Decorator that enforces a rate limit on the wrapped function.

    Args:
        limiter:        A configured :class:`~ratelimiter.algorithms.base.BaseAlgorithm`.
        key:            Static key to use for all callers.  Mutually exclusive
                        with *key_func*.  Defaults to the function's qualified name.
        key_func:       Callable that receives the same ``(*args, **kwargs)``
                        as the decorated function and returns a string key.
        cost:           Request cost (tokens consumed per call).  Default 1.
        raise_on_limit: If ``True`` (default), raise
                        :class:`~ratelimiter.exceptions.RateLimitExceeded`.
                        If ``False``, return ``None`` instead of calling the
                        wrapped function.

    Returns:
        A decorated function that enforces the rate limit.

    Example::

        from ratelimiter.backends.memory import MemoryBackend
        from ratelimiter.algorithms.token_bucket import TokenBucketRateLimiter
        from ratelimiter.decorators import rate_limit

        limiter = TokenBucketRateLimiter(MemoryBackend(), limit=10, window=1)

        @rate_limit(limiter, key_func=lambda uid, **kw: f"user:{uid}")
        def fetch_data(uid: str) -> dict:
            return {"uid": uid}
    """
    if key is not None and key_func is not None:
        raise ValueError("Specify at most one of 'key' or 'key_func'.")

    def decorator(func: F) -> F:
        resolved_key = key or func.__qualname__

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            effective_key = key_func(*args, **kwargs) if key_func else resolved_key
            result: RateLimitResult = limiter.is_allowed(effective_key, cost)

            if not result.allowed:
                if raise_on_limit:
                    raise RateLimitExceeded(
                        key=effective_key,
                        limit=result.limit,
                        window=limiter.window,
                        retry_after=result.retry_after,
                    )
                return None

            return func(*args, **kwargs)

        # Attach the limiter for introspection / testing
        wrapper.limiter = limiter  # type: ignore[attr-defined]
        return wrapper  # type: ignore[return-value]

    return decorator


class RateLimitContext:
    """Context manager wrapper around any :class:`~ratelimiter.algorithms.base.BaseAlgorithm`.

    Raises :class:`~ratelimiter.exceptions.RateLimitExceeded` on ``__enter__``
    if the limit is breached.

    Example::

        with RateLimitContext(limiter, key="user:42"):
            do_work()
    """

    def __init__(
        self,
        limiter: BaseAlgorithm,
        key: str,
        cost: int = 1,
    ) -> None:
        self.limiter = limiter
        self.key = key
        self.cost = cost
        self.result: Optional[RateLimitResult] = None

    def __enter__(self) -> "RateLimitContext":
        self.result = self.limiter.is_allowed(self.key, self.cost)
        if not self.result.allowed:
            raise RateLimitExceeded(
                key=self.key,
                limit=self.result.limit,
                window=self.limiter.window,
                retry_after=self.result.retry_after,
            )
        return self

    def __exit__(self, *_: Any) -> None:
        pass
