"""
Base class and result type shared by all rate-limiting algorithms.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

from ..backends.base import BaseBackend

if TYPE_CHECKING:
    from ..config import ConfigProvider


@dataclass(frozen=True)
class RateLimitResult:
    """Outcome of a single rate-limit check.

    Attributes:
        allowed:     Whether the request is permitted.
        key:         The rate-limit key that was checked.
        limit:       Maximum requests allowed in the window.
        remaining:   Requests still allowed before the limit is hit.
        reset_after: Seconds until the current window / quota resets.
        retry_after: Seconds to wait before retrying (0 if *allowed*).
        metadata:    Extra algorithm-specific info (e.g. token count).
    """

    allowed: bool
    key: str
    limit: int
    remaining: int
    reset_after: float
    retry_after: float = 0.0
    metadata: dict = field(default_factory=dict)

    # Convenience helpers -----------------------------------------------

    @property
    def headers(self) -> dict[str, str]:
        """Return standard ``X-RateLimit-*`` HTTP headers."""
        h = {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(int(self.reset_after)),
        }
        if not self.allowed:
            h["Retry-After"] = f"{self.retry_after:.2f}"
        return h


class BaseAlgorithm(ABC):
    """Abstract base for all rate-limiting algorithms.

    Args:
        backend:  Storage backend to use.
        limit:    Maximum number of requests allowed per window.
        window:   Time window in seconds.
        key_prefix: Optional prefix added to every key.
    """

    def __init__(
        self,
        backend: BaseBackend,
        limit: int,
        window: float,
        key_prefix: str = "",
        config_provider: Optional["ConfigProvider"] = None,
    ) -> None:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if window <= 0:
            raise ValueError("window must be a positive number")

        self.backend = backend
        self.limit = limit
        self.window = window
        self.key_prefix = key_prefix
        self._config_provider = config_provider

    def _full_key(self, key: str) -> str:
        return f"{self.key_prefix}{key}" if self.key_prefix else key

    def _refresh_config(self) -> None:
        """Pull the latest limit/window from the config provider, if set.

        Algorithms should call this at the top of :meth:`is_allowed` to
        support runtime configuration changes via :class:`~ratelimiter.config.DynamicConfig`.
        """
        if self._config_provider is not None:
            self.limit = self._config_provider.get_limit()
            self.window = self._config_provider.get_window()

    @abstractmethod
    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        """Check whether the request identified by *key* is allowed.

        Args:
            key:  Unique identifier (user ID, IP address, API key, …).
            cost: How many tokens/requests this call consumes (default 1).

        Returns:
            A :class:`RateLimitResult` describing the outcome.
        """

    def reset(self, key: str) -> None:
        """Clear all rate-limit state for *key*."""
        self.backend.delete(self._full_key(key))
