"""
Thread-safe in-memory storage backend.

Suitable for single-process applications and testing.  State is lost
when the process exits.  For multi-process deployments use Redis or SQLite.

Uses 16 independent shards (each with its own lock) so unrelated keys never
contend.  All operations on a single key are still atomic.
"""

from __future__ import annotations

import bisect
import threading
import time
from collections import defaultdict
from typing import Any, Optional

from .base import BaseBackend

_NUM_SHARDS = 16
_MAX_MEMBER = "\xff"  # sentinel larger than any real member string


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

    State is split across ``_NUM_SHARDS`` independent shards.  Operations on
    different keys that land in different shards proceed concurrently without
    any lock contention.  Operations on the same key always use the same shard
    and remain fully atomic.

    Example::

        from smart_ratelimiter.backends.memory import MemoryBackend
        backend = MemoryBackend()
    """

    def __init__(self) -> None:
        self._locks: list[threading.Lock] = [threading.Lock() for _ in range(_NUM_SHARDS)]
        # Per-shard KV stores
        self._stores: list[dict[str, _Entry]] = [{} for _ in range(_NUM_SHARDS)]
        # Per-shard sorted sets: key -> sorted list of (score, member)
        self._zsets: list[dict[str, list[tuple[float, str]]]] = [
            defaultdict(list) for _ in range(_NUM_SHARDS)
        ]
        # Per-shard member lookup: key -> {member: score} for O(1) zadd dedup
        self._zmembers: list[dict[str, dict[str, float]]] = [
            defaultdict(dict) for _ in range(_NUM_SHARDS)
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _shard(self, key: str) -> int:
        return hash(key) % _NUM_SHARDS

    def _get_entry(self, store: dict[str, _Entry], key: str) -> Optional[_Entry]:
        entry = store.get(key)
        if entry is None:
            return None
        if entry.expired:
            del store[key]
            return None
        return entry

    # ------------------------------------------------------------------
    # BaseBackend implementation
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        s = self._shard(key)
        with self._locks[s]:
            entry = self._get_entry(self._stores[s], key)
            return entry.value if entry else None

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        expires_at = time.monotonic() + ttl if ttl is not None else None
        s = self._shard(key)
        with self._locks[s]:
            self._stores[s][key] = _Entry(value, expires_at)

    def delete(self, key: str) -> None:
        s = self._shard(key)
        with self._locks[s]:
            self._stores[s].pop(key, None)
            self._zsets[s].pop(key, None)
            self._zmembers[s].pop(key, None)

    def incr(self, key: str, amount: int = 1) -> int:
        s = self._shard(key)
        with self._locks[s]:
            entry = self._get_entry(self._stores[s], key)
            current = int(entry.value) if entry else 0
            new_value = current + amount
            expires_at = entry.expires_at if entry else None
            self._stores[s][key] = _Entry(new_value, expires_at)
            return new_value

    def expire(self, key: str, ttl: float) -> None:
        s = self._shard(key)
        with self._locks[s]:
            entry = self._get_entry(self._stores[s], key)
            if entry:
                entry.expires_at = time.monotonic() + ttl

    # ------------------------------------------------------------------
    # Sorted-set operations
    # ------------------------------------------------------------------

    def zadd(self, key: str, score: float, member: str) -> None:
        s = self._shard(key)
        with self._locks[s]:
            zset = self._zsets[s][key]
            members = self._zmembers[s][key]
            old_score = members.get(member)
            if old_score is not None:
                lo = bisect.bisect_left(zset, (old_score, member))
                hi = bisect.bisect_right(zset, (old_score, member))
                for i in range(lo, hi):
                    if zset[i][1] == member:
                        del zset[i]
                        break
            bisect.insort(zset, (score, member))
            members[member] = score

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        s = self._shard(key)
        with self._locks[s]:
            zset = self._zsets[s][key]
            lo = bisect.bisect_left(zset, (min_score,))
            hi = bisect.bisect_right(zset, (max_score, _MAX_MEMBER))
            removed = hi - lo
            if removed:
                members = self._zmembers[s][key]
                for _, m in zset[lo:hi]:
                    del members[m]
                del zset[lo:hi]
            return removed

    def zcard(self, key: str) -> int:
        s = self._shard(key)
        with self._locks[s]:
            return len(self._zsets[s][key])

    def zrange_by_score(
        self, key: str, min_score: float, max_score: float
    ) -> list[tuple[str, float]]:
        s = self._shard(key)
        with self._locks[s]:
            zset = self._zsets[s][key]
            lo = bisect.bisect_left(zset, (min_score,))
            hi = bisect.bisect_right(zset, (max_score, _MAX_MEMBER))
            return [(m, sc) for sc, m in zset[lo:hi]]

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        # Acquire all shard locks in index order to avoid deadlocks.
        for lock in self._locks:
            lock.acquire()
        try:
            for s in range(_NUM_SHARDS):
                self._stores[s].clear()
                self._zsets[s].clear()
                self._zmembers[s].clear()
        finally:
            for lock in self._locks:
                lock.release()

    def clear(self) -> None:
        """Remove all keys — useful in tests."""
        self.close()
