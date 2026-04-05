# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [0.1.0] — 2026-04-04

### Added

- **Five rate-limiting algorithms:**
  - `FixedWindowRateLimiter` — simple counter-per-window
  - `SlidingWindowRateLimiter` — timestamped log, no boundary bursts
  - `TokenBucketRateLimiter` — burst-tolerant with configurable refill rate
  - `LeakyBucketRateLimiter` — perfectly smooth output rate
  - `AdaptiveRateLimiter` — sliding window + token bucket + traffic-aware auto-tuning

- **Three pluggable storage backends:**
  - `MemoryBackend` — thread-safe in-process store
  - `RedisBackend` — distributed store (requires `redis` extra)
  - `SQLiteBackend` — zero-dependency persistent store with WAL mode

- **`BaseBackend` ABC** for custom backend implementations

- **`RateLimitResult`** dataclass with `.headers` property for standard HTTP headers

- **`@rate_limit` decorator** with static key, dynamic `key_func`, cost, and `raise_on_limit` options

- **`RateLimitContext`** context manager

- **`RateLimitMiddleware`** — PEP 3333 WSGI middleware

- **`AsyncRateLimitMiddleware`** — ASGI middleware compatible with FastAPI and Starlette

- Full type annotations and `py.typed` marker

- Comprehensive test suite covering all algorithms and backends
