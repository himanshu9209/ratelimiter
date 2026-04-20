# Changelog

All notable changes to smart-ratelimiter are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Changed
- **Breaking:** Package import renamed from `ratelimiter` to `smart_ratelimiter` to avoid collision with the existing `ratelimiter` package on PyPI. Update all imports accordingly.

### Added
- `SlidingWindowCounterRateLimiter` — O(1) memory approximation algorithm (two-bucket weighted blend, ~99% accurate).
- `AsyncRateLimiter` and `async_rate_limit` decorator for native async usage via thread-pool executor.
- `DynamicConfig` and `ConfigProvider` protocol for runtime limit/window changes without restart.
- `InMemoryMetricsCollector` and `ObservableRateLimiter` for per-key allowed/dropped observability.
- `wsgi_ip_func`, `wsgi_api_key_func`, `wsgi_composite_key_func` — WSGI client identification helpers.
- `asgi_ip_func`, `asgi_api_key_func`, `asgi_composite_key_func` — ASGI client identification helpers.
- `AsyncRedisBackend` — fully async Redis backend using `redis.asyncio`.
- `examples/redis_distributed.py` — complete multi-process FastAPI example with shared Redis state.
- Property-based tests (Hypothesis) covering invariants across all six algorithms.
- Full middleware test suite (WSGI + ASGI) with raw protocol drivers — no framework dependency.
- Full Redis backend conformance test suite using `fakeredis`.

### Changed
- `MemoryBackend` sorted-set operations rewritten with `bisect` module: `zadd` uses `bisect.insort` (O(log N)), `zremrangebyscore` and `zrange_by_score` use `bisect_left`/`bisect_right` (O(log N)) instead of O(N) list comprehensions.
- `MemoryBackend` now uses 16 independent shard locks (keyed by `hash(key) % 16`) instead of a single global lock, eliminating contention between unrelated keys under concurrent load.
- `AdaptiveRateLimiter._effective_burst()` result cached with TTL `min(1.0, window/10)` to avoid sorted-set scan on every request.
- `SlidingWindowRateLimiter` and `AdaptiveRateLimiter` replace `uuid.uuid4().hex` with `itertools.count()` for sorted-set member IDs, saving ~9µs per `zadd` call.
- Benchmark script (`benchmark.py`) extended with multi-threaded concurrency suite showing scaling behaviour across 1/2/4/8 threads.

### Performance (in-memory backend, Python 3.12)
| Algorithm | Before | After |
|---|---:|---:|
| Adaptive | 880 ops/s | 70,574 ops/s |
| SlidingWindow | 1,698 ops/s | 4,245 ops/s |
| FixedWindow | 117,000 ops/s | 117,297 ops/s |

---

## [0.1.0] — 2026-04-04

Initial public release.

### Added

**Algorithms**
- `FixedWindowRateLimiter` — O(1) counter per time bucket. Cheapest algorithm; has boundary-burst vulnerability.
- `SlidingWindowRateLimiter` — Sorted-set timestamp log. Exact accuracy, no boundary burst, O(N) memory per key.
- `TokenBucketRateLimiter` — Refilling token reservoir. Burst-tolerant; enforces sustained average rate.
- `LeakyBucketRateLimiter` — Constant-drain bucket. Perfectly smooth output; zero burst tolerance.
- `AdaptiveRateLimiter` — Three-layer hybrid: sliding window guard + token bucket + real-time load sensor.

**Backends**
- `MemoryBackend` — Thread-safe in-process storage.
- `SQLiteBackend` — Persistent single-host backend with WAL mode.
- `RedisBackend` — Distributed backend for multi-process deployments.

**Integrations**
- `RateLimitMiddleware` — PEP 3333 WSGI middleware.
- `AsyncRateLimitMiddleware` — ASGI middleware for FastAPI / Starlette.
- `rate_limit` decorator with static key, `key_func`, cost, and `raise_on_limit`.
- `RateLimitContext` context manager.

**Design decisions**

*Why a separate `AdaptiveRateLimiter` rather than making all limiters adaptive?*
The adaptive algorithm requires a global load sensor (a shared sorted set across all keys). Embedding it in `BaseAlgorithm` would force every algorithm to pay that overhead. The separate class keeps each limiter's hot path lean.

*Why `bisect` instead of `sortedcontainers.SortedList`?*
Zero required dependencies is a hard constraint. The standard-library `bisect` module covers the sorted-set semantics needed by sliding-window algorithms without adding a runtime dependency.

*Why 16 shards in `MemoryBackend`?*
16 eliminates most contention for typical rate-limiting workloads (tens of distinct keys per process) while keeping the overhead of pre-allocated dicts negligible. The shard count is `_NUM_SHARDS` — tunable without changing the public interface.

*Why thread-pool executor for `AsyncRateLimiter`?*
The sync algorithms use `threading.Lock` internally. Rewriting them as native async would require a parallel implementation of every algorithm and backend. The executor approach reuses all existing code and is correct for `MemoryBackend` workloads. Native async is provided via `AsyncRedisBackend` for I/O-bound production use.

---

[Unreleased]: https://github.com/himanshu9209/smart-ratelimiter/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/himanshu9209/smart-ratelimiter/releases/tag/v0.1.0
