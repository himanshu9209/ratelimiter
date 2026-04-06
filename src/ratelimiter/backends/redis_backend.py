"""
Redis storage backend.

Requires the ``redis`` extra::

    pip install smart-ratelimiter[redis]

All operations use Lua scripts for atomicity where needed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast

from ..exceptions import BackendConnectionError, BackendError
from .base import BaseBackend

if TYPE_CHECKING:
    import redis as redis_lib


class RedisBackend(BaseBackend):
    """Redis-backed storage for distributed rate limiting.

    Args:
        client: A ``redis.Redis`` (or ``fakeredis.FakeRedis``) instance.
                If *None*, a default ``redis.Redis()`` client is created.
        key_prefix: Optional string prepended to every key.

    Example::

        import redis
        from ratelimiter.backends.redis_backend import RedisBackend

        client = redis.Redis(host="localhost", port=6379, decode_responses=True)
        backend = RedisBackend(client=client)
    """

    def __init__(
        self,
        client: Optional["redis_lib.Redis"] = None,
        key_prefix: str = "rl:",
    ) -> None:
        try:
            import redis as redis_lib  # noqa: F811
        except ImportError as exc:
            raise ImportError(
                "Redis backend requires the 'redis' package: "
                "pip install smart-ratelimiter[redis]"
            ) from exc

        if client is None:
            client = redis_lib.Redis(decode_responses=True)

        self._r = client
        self._prefix = key_prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    # ------------------------------------------------------------------
    # BaseBackend implementation
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        try:
            return self._r.get(self._k(key))
        except Exception as exc:
            raise BackendError(f"Redis GET failed: {exc}") from exc

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        try:
            px = int(ttl * 1000) if ttl is not None else None
            self._r.set(self._k(key), value, px=px)
        except Exception as exc:
            raise BackendError(f"Redis SET failed: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            self._r.delete(self._k(key))
        except Exception as exc:
            raise BackendError(f"Redis DELETE failed: {exc}") from exc

    def incr(self, key: str, amount: int = 1) -> int:
        try:
            return cast(int, self._r.incrby(self._k(key), amount))
        except Exception as exc:
            raise BackendError(f"Redis INCR failed: {exc}") from exc

    def expire(self, key: str, ttl: float) -> None:
        try:
            self._r.pexpire(self._k(key), int(ttl * 1000))
        except Exception as exc:
            raise BackendError(f"Redis EXPIRE failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Sorted-set operations
    # ------------------------------------------------------------------

    def zadd(self, key: str, score: float, member: str) -> None:
        try:
            self._r.zadd(self._k(key), {member: score})
        except Exception as exc:
            raise BackendError(f"Redis ZADD failed: {exc}") from exc

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        try:
            result = self._r.zremrangebyscore(self._k(key), min_score, max_score)
            return cast(int, result)
        except Exception as exc:
            raise BackendError(f"Redis ZREMRANGEBYSCORE failed: {exc}") from exc

    def zcard(self, key: str) -> int:
        try:
            return cast(int, self._r.zcard(self._k(key)))
        except Exception as exc:
            raise BackendError(f"Redis ZCARD failed: {exc}") from exc

    def zrange_by_score(
        self, key: str, min_score: float, max_score: float
    ) -> list[tuple[str, float]]:
        try:
            result = cast(
                list[tuple[str, float]],
                self._r.zrangebyscore(self._k(key), min_score, max_score, withscores=True),
            )
            return [(m, s) for m, s in result]
        except Exception as exc:
            raise BackendError(f"Redis ZRANGEBYSCORE failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass

    def ping(self) -> bool:
        try:
            return bool(self._r.ping())
        except Exception:
            raise BackendConnectionError("Cannot reach Redis server.")
