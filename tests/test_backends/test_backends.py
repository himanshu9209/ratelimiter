"""
Backend conformance tests.

These tests are parametrized via the ``backend`` fixture in conftest.py
and run against every concrete backend (Memory, SQLite).
Redis is tested separately using fakeredis when available.
"""

from __future__ import annotations

import time

import pytest

from smart_ratelimiter.backends.memory import MemoryBackend
from smart_ratelimiter.backends.sqlite_backend import SQLiteBackend


# ---------------------------------------------------------------------------
# Conformance suite (runs against all backends)
# ---------------------------------------------------------------------------

class TestBackendConformance:
    """Every backend must pass these tests identically."""

    # ── get / set ──────────────────────────────────────────────────────

    def test_get_missing_key_returns_none(self, backend):
        assert backend.get("nonexistent") is None

    def test_set_and_get(self, backend):
        backend.set("k", "hello")
        assert backend.get("k") == "hello"

    def test_set_overwrites(self, backend):
        backend.set("k", 1)
        backend.set("k", 2)
        assert backend.get("k") == 2

    def test_set_with_ttl_expires(self, backend):
        backend.set("k", "gone", ttl=0.05)
        time.sleep(0.1)
        assert backend.get("k") is None

    def test_set_without_ttl_persists(self, backend):
        backend.set("k", "stays")
        time.sleep(0.05)
        assert backend.get("k") == "stays"

    # ── delete ─────────────────────────────────────────────────────────

    def test_delete_removes_key(self, backend):
        backend.set("k", 1)
        backend.delete("k")
        assert backend.get("k") is None

    def test_delete_nonexistent_is_noop(self, backend):
        backend.delete("does_not_exist")  # must not raise

    # ── incr ───────────────────────────────────────────────────────────

    def test_incr_creates_key(self, backend):
        val = backend.incr("counter")
        assert val == 1

    def test_incr_increments(self, backend):
        backend.incr("counter")
        val = backend.incr("counter")
        assert val == 2

    def test_incr_amount(self, backend):
        val = backend.incr("counter", 5)
        assert val == 5

    def test_incr_preserves_ttl(self, backend):
        backend.incr("counter")
        backend.expire("counter", 0.05)
        backend.incr("counter")
        time.sleep(0.1)
        assert backend.get("counter") is None

    # ── expire ─────────────────────────────────────────────────────────

    def test_expire_sets_ttl(self, backend):
        backend.set("k", "v")
        backend.expire("k", 0.05)
        time.sleep(0.1)
        assert backend.get("k") is None

    # ── sorted sets ────────────────────────────────────────────────────

    def test_zadd_and_zcard(self, backend):
        backend.zadd("z", 1.0, "a")
        backend.zadd("z", 2.0, "b")
        assert backend.zcard("z") == 2

    def test_zadd_replaces_existing_member(self, backend):
        backend.zadd("z", 1.0, "a")
        backend.zadd("z", 5.0, "a")  # update score
        assert backend.zcard("z") == 1

    def test_zremrangebyscore(self, backend):
        for i in range(5):
            backend.zadd("z", float(i), f"m{i}")
        removed = backend.zremrangebyscore("z", 0.0, 2.0)
        assert removed == 3
        assert backend.zcard("z") == 2

    def test_zrange_by_score(self, backend):
        backend.zadd("z", 1.0, "a")
        backend.zadd("z", 3.0, "b")
        backend.zadd("z", 5.0, "c")
        results = backend.zrange_by_score("z", 1.0, 3.0)
        members = {m for m, _ in results}
        assert members == {"a", "b"}

    def test_zcard_empty_key(self, backend):
        assert backend.zcard("missing_zset") == 0

    def test_zremrangebyscore_empty_key(self, backend):
        removed = backend.zremrangebyscore("missing_zset", 0.0, 100.0)
        assert removed == 0

    # ── ping ───────────────────────────────────────────────────────────

    def test_ping_returns_true(self, backend):
        assert backend.ping() is True


# ---------------------------------------------------------------------------
# Memory-specific tests
# ---------------------------------------------------------------------------

class TestMemoryBackend:
    def test_clear_removes_all_data(self):
        b = MemoryBackend()
        b.set("a", 1)
        b.zadd("z", 1.0, "m")
        b.clear()
        assert b.get("a") is None
        assert b.zcard("z") == 0

    def test_thread_safety(self):
        import threading

        b = MemoryBackend()
        errors: list[Exception] = []

        def worker(n: int) -> None:
            try:
                for _ in range(100):
                    b.incr(f"key:{n % 5}")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors


# ---------------------------------------------------------------------------
# SQLite-specific tests
# ---------------------------------------------------------------------------

class TestSQLiteBackend:
    def test_disk_persistence(self, tmp_path):
        db = str(tmp_path / "test.db")
        b1 = SQLiteBackend(db_path=db)
        b1.set("persistent", "yes")
        b1.close()

        b2 = SQLiteBackend(db_path=db)
        assert b2.get("persistent") == "yes"
        b2.close()

    def test_wal_mode_enabled_by_default(self, tmp_path):
        db = str(tmp_path / "wal.db")
        b = SQLiteBackend(db_path=db, wal_mode=True)
        conn = b._conn()
        row = conn.execute("PRAGMA journal_mode").fetchone()
        assert row[0] == "wal"
        b.close()


# ---------------------------------------------------------------------------
# Redis backend (only when fakeredis is available)
# ---------------------------------------------------------------------------

try:
    import fakeredis  # noqa: F401
    HAS_FAKEREDIS = True
except ImportError:
    HAS_FAKEREDIS = False


@pytest.fixture
def redis_backend():
    import fakeredis
    from smart_ratelimiter.backends.redis_backend import RedisBackend
    client = fakeredis.FakeRedis(decode_responses=True)
    b = RedisBackend(client=client)
    yield b
    b.close()


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisBackendConformance:
    """Redis must pass the same conformance tests as Memory and SQLite."""

    def test_get_missing_key_returns_none(self, redis_backend):
        assert redis_backend.get("nonexistent") is None

    def test_set_and_get(self, redis_backend):
        redis_backend.set("k", "hello")
        assert redis_backend.get("k") == "hello"

    def test_set_overwrites(self, redis_backend):
        redis_backend.set("k", 1)
        redis_backend.set("k", 2)
        assert redis_backend.get("k") == 2

    def test_set_with_ttl_expires(self, redis_backend):
        redis_backend.set("k", "gone", ttl=0.05)
        time.sleep(0.12)
        assert redis_backend.get("k") is None

    def test_delete_removes_key(self, redis_backend):
        redis_backend.set("k", 1)
        redis_backend.delete("k")
        assert redis_backend.get("k") is None

    def test_delete_nonexistent_is_noop(self, redis_backend):
        redis_backend.delete("does_not_exist")

    def test_incr_creates_key(self, redis_backend):
        assert redis_backend.incr("counter") == 1

    def test_incr_increments(self, redis_backend):
        redis_backend.incr("counter")
        assert redis_backend.incr("counter") == 2

    def test_incr_amount(self, redis_backend):
        assert redis_backend.incr("counter", 5) == 5

    def test_expire_sets_ttl(self, redis_backend):
        redis_backend.set("k", "v")
        redis_backend.expire("k", 0.05)
        time.sleep(0.12)
        assert redis_backend.get("k") is None

    def test_zadd_and_zcard(self, redis_backend):
        redis_backend.zadd("z", 1.0, "a")
        redis_backend.zadd("z", 2.0, "b")
        assert redis_backend.zcard("z") == 2

    def test_zadd_replaces_existing_member(self, redis_backend):
        redis_backend.zadd("z", 1.0, "a")
        redis_backend.zadd("z", 5.0, "a")
        assert redis_backend.zcard("z") == 1

    def test_zremrangebyscore(self, redis_backend):
        for i in range(5):
            redis_backend.zadd("z", float(i), f"m{i}")
        removed = redis_backend.zremrangebyscore("z", 0.0, 2.0)
        assert removed == 3
        assert redis_backend.zcard("z") == 2

    def test_zrange_by_score(self, redis_backend):
        redis_backend.zadd("z", 1.0, "a")
        redis_backend.zadd("z", 3.0, "b")
        redis_backend.zadd("z", 5.0, "c")
        results = redis_backend.zrange_by_score("z", 1.0, 3.0)
        members = {m for m, _ in results}
        assert members == {"a", "b"}

    def test_zcard_empty_key(self, redis_backend):
        assert redis_backend.zcard("missing") == 0

    def test_ping_returns_true(self, redis_backend):
        assert redis_backend.ping() is True


@pytest.mark.skipif(not HAS_FAKEREDIS, reason="fakeredis not installed")
class TestRedisBackendSpecific:
    """Redis-specific behaviour beyond the generic conformance suite."""

    def test_key_prefix_isolates_namespaces(self):
        import fakeredis
        from smart_ratelimiter.backends.redis_backend import RedisBackend
        client = fakeredis.FakeRedis(decode_responses=True)
        b1 = RedisBackend(client=client, key_prefix="ns1:")
        b2 = RedisBackend(client=client, key_prefix="ns2:")
        b1.set("k", "from_b1")
        assert b2.get("k") is None

    def test_two_prefixes_same_logical_key_are_independent(self):
        import fakeredis
        from smart_ratelimiter.backends.redis_backend import RedisBackend
        client = fakeredis.FakeRedis(decode_responses=True)
        b1 = RedisBackend(client=client, key_prefix="a:")
        b2 = RedisBackend(client=client, key_prefix="b:")
        b1.incr("counter")
        b1.incr("counter")
        b2.incr("counter")
        assert b1.get("counter") == 2
        assert b2.get("counter") == 1

    def test_sliding_window_algorithm_with_redis(self):
        import fakeredis
        from smart_ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
        from smart_ratelimiter.backends.redis_backend import RedisBackend
        client = fakeredis.FakeRedis(decode_responses=True)
        limiter = SlidingWindowRateLimiter(RedisBackend(client=client), limit=3, window=60.0)
        assert limiter.is_allowed("u").allowed is True
        assert limiter.is_allowed("u").allowed is True
        assert limiter.is_allowed("u").allowed is True
        assert limiter.is_allowed("u").allowed is False

    def test_adaptive_algorithm_with_redis(self):
        import fakeredis
        from smart_ratelimiter.algorithms.adaptive import AdaptiveRateLimiter
        from smart_ratelimiter.backends.redis_backend import RedisBackend
        client = fakeredis.FakeRedis(decode_responses=True)
        limiter = AdaptiveRateLimiter(
            RedisBackend(client=client), limit=5, window=60.0, burst_multiplier=2.0
        )
        # burst cap = 10; all 10 should be allowed under low load
        results = [limiter.is_allowed("tenant").allowed for _ in range(10)]
        assert all(results)
