"""
Custom exceptions for smart-ratelimiter.
"""

from __future__ import annotations


class RateLimitError(Exception):
    """Base exception for all rate limiting errors."""


class RateLimitExceeded(RateLimitError):
    """Raised when a rate limit has been exceeded.

    Attributes:
        key: The rate limit key that was exceeded.
        limit: The maximum number of requests allowed.
        window: The time window in seconds.
        retry_after: Seconds to wait before the next request is allowed.
    """

    def __init__(
        self,
        key: str,
        limit: int,
        window: float,
        retry_after: float,
    ) -> None:
        self.key = key
        self.limit = limit
        self.window = window
        self.retry_after = retry_after
        super().__init__(
            f"Rate limit exceeded for key '{key}': "
            f"{limit} requests per {window}s. "
            f"Retry after {retry_after:.2f}s."
        )


class BackendError(RateLimitError):
    """Raised when a storage backend operation fails."""


class ConfigurationError(RateLimitError):
    """Raised for invalid rate limiter configuration."""


class BackendConnectionError(BackendError):
    """Raised when a backend cannot be connected to."""
