"""
Abstract base class for rate limiter storage backends.

All backends must implement this interface, enabling algorithms to be
fully decoupled from storage concerns.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional


class BaseBackend(ABC):
    """Abstract storage backend for rate limiter state.

    Implementations must be thread-safe. For async use, wrap in an
    executor or provide an async subclass.
    """

    # ------------------------------------------------------------------
    # Core atomic operations
    # ------------------------------------------------------------------

    @abstractmethod
    def get(self, key: str) -> Optional[Any]:
        """Return the value stored at *key*, or ``None`` if absent."""

    @abstractmethod
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store *value* at *key* with an optional TTL in seconds."""

    @abstractmethod
    def delete(self, key: str) -> None:
        """Remove *key* from the store (no-op if absent)."""

    @abstractmethod
    def incr(self, key: str, amount: int = 1) -> int:
        """Atomically increment an integer counter and return the new value.

        If the key does not exist it is initialised to 0 before incrementing.
        """

    @abstractmethod
    def expire(self, key: str, ttl: float) -> None:
        """Set (or reset) the TTL on an existing key."""

    # ------------------------------------------------------------------
    # Sorted-set operations (used by sliding-window log)
    # ------------------------------------------------------------------

    @abstractmethod
    def zadd(self, key: str, score: float, member: str) -> None:
        """Add *member* with *score* to a sorted set."""

    @abstractmethod
    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        """Remove members whose score is between *min_score* and *max_score* (inclusive).

        Returns the number of members removed.
        """

    @abstractmethod
    def zcard(self, key: str) -> int:
        """Return the number of members in the sorted set."""

    @abstractmethod
    def zrange_by_score(
        self, key: str, min_score: float, max_score: float
    ) -> list[tuple[str, float]]:
        """Return ``(member, score)`` pairs with score in [*min_score*, *max_score*]."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release any resources held by this backend.  Override as needed."""

    def ping(self) -> bool:
        """Return ``True`` if the backend is reachable.  Override as needed."""
        return True
