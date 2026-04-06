"""
Dynamic configuration for rate limiters.

Allows ``limit`` and ``window`` to be updated at runtime — without
restarting the service or creating a new limiter instance — by
attaching a :class:`ConfigProvider` to any algorithm.

Quick start::

    from ratelimiter.config import DynamicConfig
    from ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
    from ratelimiter.backends.memory import MemoryBackend

    cfg = DynamicConfig(limit=100, window=60)
    limiter = SlidingWindowRateLimiter(
        MemoryBackend(), limit=100, window=60, config_provider=cfg
    )

    # Later, with no restart:
    cfg.update(limit=200)   # doubles the rate limit immediately
"""

from __future__ import annotations

import threading
from typing import Optional

try:
    from typing import Protocol, runtime_checkable
except ImportError:  # Python < 3.8
    from typing_extensions import Protocol, runtime_checkable  # type: ignore[assignment]


@runtime_checkable
class ConfigProvider(Protocol):
    """Protocol for objects that supply runtime rate-limit configuration.

    Any object that implements ``get_limit()`` and ``get_window()``
    satisfies this protocol — you are not required to subclass anything.
    """

    def get_limit(self) -> int:
        """Return the current maximum number of requests per window."""
        ...

    def get_window(self) -> float:
        """Return the current window duration in seconds."""
        ...


class DynamicConfig:
    """Thread-safe, mutable configuration provider.

    Create one instance and share it across one or more limiter
    instances.  Call :meth:`update` at any point to change the active
    limit or window; the next ``is_allowed`` call on every attached
    limiter will pick up the new values automatically.

    Args:
        limit:  Initial maximum requests per window.
        window: Initial window duration in seconds.

    Example::

        cfg = DynamicConfig(limit=60, window=60)
        limiter = FixedWindowRateLimiter(
            MemoryBackend(), limit=60, window=60, config_provider=cfg
        )

        # Tighten limits at peak hours without a restart:
        cfg.update(limit=30)
    """

    def __init__(self, limit: int, window: float) -> None:
        if limit <= 0:
            raise ValueError("limit must be a positive integer")
        if window <= 0:
            raise ValueError("window must be a positive number")
        self._lock = threading.Lock()
        self._limit = limit
        self._window = window

    # ------------------------------------------------------------------
    # ConfigProvider protocol
    # ------------------------------------------------------------------

    def get_limit(self) -> int:
        with self._lock:
            return self._limit

    def get_window(self) -> float:
        with self._lock:
            return self._window

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------

    def update(
        self,
        limit: Optional[int] = None,
        window: Optional[float] = None,
    ) -> None:
        """Update configuration values at runtime.

        Args:
            limit:  New request ceiling.  ``None`` keeps the current value.
            window: New window duration in seconds.  ``None`` keeps the
                    current value.

        Raises:
            ValueError: If the supplied values are not positive.
        """
        with self._lock:
            if limit is not None:
                if limit <= 0:
                    raise ValueError("limit must be a positive integer")
                self._limit = limit
            if window is not None:
                if window <= 0:
                    raise ValueError("window must be a positive number")
                self._window = window

    def __repr__(self) -> str:
        return f"DynamicConfig(limit={self._limit}, window={self._window})"
