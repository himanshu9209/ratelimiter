"""
Fully async Redis backend using ``redis.asyncio``.

Unlike :class:`~ratelimiter.backends.redis_backend.RedisBackend`, this
backend never blocks the event loop.  Use it with :class:`AsyncRateLimiter`
or implement your own ``async`` algorithm layer.

Requires::

    pip install smart-ratelimiter[redis]

Example::

    import redis.asyncio as aioredis
    from smart_ratelimiter.backends.async_redis_backend import AsyncRedisBackend
    from smart_ratelimiter.algorithms.async_base import AsyncRateLimiter
    from smart_ratelimiter.algorithms.adaptive import AdaptiveRateLimiter

    async def main():
        client = aioredis.Redis(host="localhost", decode_responses=True)
        backend = AsyncRedisBackend(client=client)

        # Wrap a sync algorithm — the backend calls become coroutines
        # Note: for true async, use AsyncRedisBackend with AsyncAdaptiveRateLimiter
        # (not yet implemented — this backend is provided for custom async use)
        await backend.set("key", "value", ttl=10)
        print(await backend.get("key"))
        await client.aclose()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from ..exceptions import BackendConnectionError, BackendError

if TYPE_CHECKING:
    import redis.asyncio as aioredis


class AsyncRedisBackend:
    """Async Redis backend using ``redis.asyncio``.

    All methods are coroutines — call with ``await``.

    Args:
        client:     A ``redis.asyncio.Redis`` instance.
        key_prefix: Optional string prepended to every key.
    """

    def __init__(
        self,
        client: "aioredis.Redis",
        key_prefix: str = "rl:",
    ) -> None:
        try:
            import redis.asyncio  # noqa: F401
        except ImportError as exc:
            raise ImportError(
                "Async Redis backend requires the 'redis' package: "
                "pip install smart-ratelimiter[redis]"
            ) from exc

        self._r = client
        self._prefix = key_prefix

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    # ------------------------------------------------------------------
    # KV operations
    # ------------------------------------------------------------------

    async def get(self, key: str) -> Optional[Any]:
        try:
            return await self._r.get(self._k(key))
        except Exception as exc:
            raise BackendError(f"Async Redis GET failed: {exc}") from exc

    async def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        try:
            px = int(ttl * 1000) if ttl is not None else None
            await self._r.set(self._k(key), value, px=px)
        except Exception as exc:
            raise BackendError(f"Async Redis SET failed: {exc}") from exc

    async def delete(self, key: str) -> None:
        try:
            await self._r.delete(self._k(key))
        except Exception as exc:
            raise BackendError(f"Async Redis DELETE failed: {exc}") from exc

    async def incr(self, key: str, amount: int = 1) -> int:
        try:
            return int(await self._r.incrby(self._k(key), amount))
        except Exception as exc:
            raise BackendError(f"Async Redis INCR failed: {exc}") from exc

    async def expire(self, key: str, ttl: float) -> None:
        try:
            await self._r.pexpire(self._k(key), int(ttl * 1000))
        except Exception as exc:
            raise BackendError(f"Async Redis EXPIRE failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Sorted-set operations
    # ------------------------------------------------------------------

    async def zadd(self, key: str, score: float, member: str) -> None:
        try:
            await self._r.zadd(self._k(key), {member: score})
        except Exception as exc:
            raise BackendError(f"Async Redis ZADD failed: {exc}") from exc

    async def zremrangebyscore(
        self, key: str, min_score: float, max_score: float
    ) -> int:
        try:
            result = await self._r.zremrangebyscore(self._k(key), min_score, max_score)
            return int(result)
        except Exception as exc:
            raise BackendError(f"Async Redis ZREMRANGEBYSCORE failed: {exc}") from exc

    async def zcard(self, key: str) -> int:
        try:
            return int(await self._r.zcard(self._k(key)))
        except Exception as exc:
            raise BackendError(f"Async Redis ZCARD failed: {exc}") from exc

    async def zrange_by_score(
        self, key: str, min_score: float, max_score: float
    ) -> list[tuple[str, float]]:
        try:
            result = await self._r.zrangebyscore(
                self._k(key), min_score, max_score, withscores=True
            )
            return [(m, s) for m, s in result]
        except Exception as exc:
            raise BackendError(f"Async Redis ZRANGEBYSCORE failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        try:
            await self._r.aclose()
        except Exception:
            pass

    async def ping(self) -> bool:
        try:
            return bool(await self._r.ping())  # type: ignore[misc]
        except Exception:
            raise BackendConnectionError("Cannot reach async Redis server.")
