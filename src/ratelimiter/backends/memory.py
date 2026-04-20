"""
Thread-safe in-memory storage backend.

Suitable for single-process applications and testing.  State is lost
when the process exits.  For multi-process deployments use Redis or SQLite.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import defaultdict
from typing import Any, Optional

_MAX_MEMBER = "\xff"  # sentinel larger than any real member string

from .base import BaseBackend


class _Entry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: Any, expires_at: Optional[float]) -> None:
        self.value = value
        self.expires_at = expires_at

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and time.monotonic() > self.expires_at


class MemoryBackend(BaseBackend):
    """In-process, thread-safe backend backed by plain Python dicts.

    Example::

        from ratelimiter.backends.memory import MemoryBackend
        backend = MemoryBackend()
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: dict[str, _Entry] = {}
        # sorted sets: key -> list of (score, member), kept sorted by (score, member)
        self._zsets: dict[str, list[tuple[float, str]]] = defaultdict(list)
        # member lookup: key -> {member: score} for O(1) existence check in zadd
        self._zmembers: dict[str, dict[str, float]] = defaultdict(dict)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_entry(self, key: str) -> Optional[_Entry]:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expired:
            del self._store[key]
            return None
        return entry

    # ------------------------------------------------------------------
    # BaseBackend implementation
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        with self._lock:
            entry = self._get_entry(key)
            return entry.value if entry else None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expires_at = time.monotonic() + ttl if ttl is not None else None
        with self._lock:
            self._store[key] = _Entry(value, expires_at)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)
            self._zsets.pop(key, None)
            self._zmembers.pop(key, None)

    def incr(self, key: str, amount: int = 1) -> int:
        with self._lock:
            entry = self._get_entry(key)
            current = int(entry.value) if entry else 0
            new_value = current + amount
            expires_at = entry.expires_at if entry else None
            self._store[key] = _Entry(new_value, expires_at)
            return new_value

    def expire(self, key: str, ttl: float) -> None:
        with self._lock:
            entry = self._get_entry(key)
            if entry:
                entry.expires_at = time.monotonic() + ttl

    # ------------------------------------------------------------------
    # Sorted-set operations
    # ------------------------------------------------------------------

    def zadd(self, key: str, score: float, member: str) -> None:
        with self._lock:
            zset = self._zsets[key]
            members = self._zmembers[key]
            old_score = members.get(member)
            if old_score is not None:
                # Remove the stale entry at its old position (O(log N) find + O(N) shift)
                lo = bisect.bisect_left(zset, (old_score, member))
                hi = bisect.bisect_right(zset, (old_score, member))
                for i in range(lo, hi):
                    if zset[i][1] == member:
                        del zset[i]
                        break
            bisect.insort(zset, (score, member))
            members[member] = score

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        with self._lock:
            zset = self._zsets[key]
            lo = bisect.bisect_left(zset, (min_score,))
            hi = bisect.bisect_right(zset, (max_score, _MAX_MEMBER))
            removed = hi - lo
            if removed:
                members = self._zmembers[key]
                for _, m in zset[lo:hi]:
                    del members[m]
                del zset[lo:hi]
            return removed

    def zcard(self, key: str) -> int:
        with self._lock:
            return len(self._zsets[key])

    def zrange_by_score(
        self, key: str, min_score: float, max_score: float
    ) -> list[tuple[str, float]]:
        with self._lock:
            zset = self._zsets[key]
            lo = bisect.bisect_left(zset, (min_score,))
            hi = bisect.bisect_right(zset, (max_score, _MAX_MEMBER))
            return [(m, s) for s, m in zset[lo:hi]]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        with self._lock:
            self._store.clear()
            self._zsets.clear()
            self._zmembers.clear()

    def clear(self) -> None:
        """Remove all keys — useful in tests."""
        self.close()
