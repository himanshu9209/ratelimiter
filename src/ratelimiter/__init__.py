"""
smart-ratelimiter
~~~~~~~~~~~~~~~~~

A flexible, pluggable API rate limiter with multiple algorithms and backends.

Quick start::

    from ratelimiter import AdaptiveRateLimiter, MemoryBackend

    limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60)
    result = limiter.is_allowed("user:42")
    if not result.allowed:
        print(f"Slow down! Retry after {result.retry_after:.1f}s")

See the README for full documentation.
"""

from .algorithms.adaptive import AdaptiveRateLimiter
from .algorithms.async_base import AsyncRateLimiter, async_rate_limit
from .algorithms.base import BaseAlgorithm, RateLimitResult
from .algorithms.fixed_window import FixedWindowRateLimiter
from .algorithms.leaky_bucket import LeakyBucketRateLimiter
from .algorithms.sliding_window import SlidingWindowRateLimiter
from .algorithms.token_bucket import TokenBucketRateLimiter
from .backends.memory import MemoryBackend
from .backends.sqlite_backend import SQLiteBackend
from .decorators import RateLimitContext, rate_limit
from .exceptions import (
    BackendConnectionError,
    BackendError,
    ConfigurationError,
    RateLimitError,
    RateLimitExceeded,
)

__all__ = [
    # Algorithms
    "BaseAlgorithm",
    "RateLimitResult",
    "FixedWindowRateLimiter",
    "SlidingWindowRateLimiter",
    "TokenBucketRateLimiter",
    "LeakyBucketRateLimiter",
    "AdaptiveRateLimiter",
    # Async
    "AsyncRateLimiter",
    "async_rate_limit",
    # Backends
    "MemoryBackend",
    "SQLiteBackend",
    # Decorators / context managers
    "rate_limit",
    "RateLimitContext",
    # Exceptions
    "RateLimitError",
    "RateLimitExceeded",
    "BackendError",
    "BackendConnectionError",
    "ConfigurationError",
]

__version__ = "0.1.0"
