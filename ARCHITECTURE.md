# Architecture

A deep-dive into how smart-ratelimiter is structured, why certain decisions were made, and where to look when something needs to change.

---

## Layer map

```
┌─────────────────────────────────────────────────────────────┐
│  Public API  (smart_ratelimiter/__init__.py)                 │
│  rate_limit decorator · RateLimitContext · AsyncRateLimiter  │
│  WSGI/ASGI middleware · ObservableRateLimiter                │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│  Algorithms  (smart_ratelimiter/algorithms/)                 │
│                                                             │
│  BaseAlgorithm (abstract)                                   │
│    ├── FixedWindowRateLimiter                               │
│    ├── SlidingWindowRateLimiter                             │
│    ├── SlidingWindowCounterRateLimiter                      │
│    ├── TokenBucketRateLimiter                               │
│    ├── LeakyBucketRateLimiter                               │
│    └── AdaptiveRateLimiter  ◄── flagship                    │
└───────────────────────────┬─────────────────────────────────┘
                            │  BaseBackend interface
┌───────────────────────────▼─────────────────────────────────┐
│  Backends  (smart_ratelimiter/backends/)                     │
│                                                             │
│  BaseBackend (abstract)                                     │
│    ├── MemoryBackend    — in-process, sharded locks          │
│    ├── SQLiteBackend    — persistent single-host, WAL        │
│    ├── RedisBackend     — distributed sync                   │
│    └── AsyncRedisBackend — distributed async                 │
└─────────────────────────────────────────────────────────────┘
```

---

## The `BaseBackend` interface

Every backend must implement exactly these methods:

```
KV store:       get · set · delete · incr · expire
Sorted sets:    zadd · zremrangebyscore · zcard · zrange_by_score
Lifecycle:      close · ping
```

The sorted-set methods exist because the sliding-window algorithms need them. They map directly to Redis ZADD/ZREMRANGEBYSCORE/ZCARD/ZRANGEBYSCORE. The in-memory and SQLite backends emulate the same semantics without Redis.

**Rule:** algorithms must only call `BaseBackend` methods. They must not reach into backend internals.

---

## Algorithm internals

### Fixed Window

```
now  →  bucket_id = int(now / window) * window
key = f"{user_key}:{int(bucket_id)}"
count = backend.incr(key)          # atomic
if count == 1: backend.expire(key, window)
allowed = count <= limit
```

One backend call on the fast path (incr). TTL set only on first request to avoid races. Cheapest algorithm — no sorted sets, no floating-point state.

**Boundary-burst problem:** a client can fire `limit` requests at time `T-ε` and `limit` more at `T+ε` — two full windows in rapid succession. This is an inherent property of the algorithm, not a bug.

### Sliding Window Log

```
zremrangebyscore(key, 0, now - window)   # prune stale entries
count = zcard(key)
if count + cost <= limit:
    for _ in range(cost):
        zadd(key, now, f"{now}:{counter}")
    expire(key, window + 1)
```

The sorted set stores one entry per request. Members are `"{timestamp}:{counter}"` to avoid score collisions (two requests at the same millisecond would otherwise overwrite each other). The counter is a module-level `itertools.count()` — thread-safe in CPython, zero overhead vs uuid4.

Memory cost: O(N) per key where N = limit. For `limit=10000`, a single active key holds 10,000 entries. Use `SlidingWindowCounterRateLimiter` or `AdaptiveRateLimiter` at high limits.

### Sliding Window Counter

```
curr_bucket = int(now / window)
prev_bucket = curr_bucket - 1
elapsed = now - curr_bucket * window
weight_prev = 1.0 - (elapsed / window)
effective = prev_count * weight_prev + curr_count
allowed = effective + cost <= limit
```

Two counters. The previous bucket's contribution is linearly weighted based on how far into the current window we are. At the start of a new window, the previous bucket counts fully; at the end, it counts zero. This approximates the exact sliding window at O(1) memory.

Accuracy: the approximation error is bounded. In the worst case (all `limit` requests fired at the very end of the previous window), the effective count slightly underestimates and allows ~1% more than the strict limit.

### Token Bucket

```
state = backend.get(tb_key) or {tokens: limit, last_refill: now}
elapsed = now - state.last_refill
tokens = min(limit, state.tokens + elapsed * refill_rate)
allowed = tokens >= cost
if allowed: tokens -= cost
backend.set(tb_key, {tokens, last_refill: now}, ttl=window * 2)
```

State is a single dict in the backend. No sorted sets. The refill is computed lazily on every request (no background thread). `refill_rate` defaults to `limit / window` but can be overridden for non-uniform bucket/rate configurations.

### Leaky Bucket

Structurally identical to token bucket but inverted: instead of tokens accumulating and being consumed, a water level rises on each request and drains at a constant rate. The effect is a perfectly smooth output: exactly `leak_rate` requests pass per second regardless of input burst shape.

### ★ Adaptive Hybrid

Three layers run on every `is_allowed()` call:

```
┌─────────────────────────────────────────────────────┐
│ Layer 0: _effective_burst()                          │
│   Read load sensor (global sorted set)              │
│   Compute measured_rate = recent_count / adap_window│
│   Interpolate penalty factor                        │
│   Cache result for burst_cache_ttl seconds          │
│   → effective_burst = max_burst * factor            │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│ Layer 1: Sliding window guard                        │
│   zremrangebyscore(sw_key, 0, now - window)         │
│   sw_count = zcard(sw_key)                          │
│   if sw_count + cost > effective_burst: REJECT      │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│ Layer 2: Token bucket                                │
│   tokens = min(effective_burst, tokens + elapsed    │
│                                  * refill_rate)     │
│   if tokens < cost: REJECT                          │
│   tokens -= cost                                    │
└──────────────────────┬──────────────────────────────┘
                       │
┌──────────────────────▼──────────────────────────────┐
│ Record                                               │
│   zadd(sw_key, now, member)                         │
│   zadd(load_key, now, member)  ← global sensor      │
│   set(tb_key, {tokens, last_refill})                │
└─────────────────────────────────────────────────────┘
```

**The global load sensor** is a sorted set keyed `{prefix}__global__:aw:load`. Every allowed request from every caller writes to this set. `_effective_burst` reads it to measure aggregate traffic rate. This means the burst cap is *global* — heavy traffic from one tenant tightens the burst for all tenants on the same limiter instance. This is intentional: the goal is system-level stability, not per-tenant fairness. If per-tenant fairness is required, create one `AdaptiveRateLimiter` per tenant.

**The burst cache** (`_burst_cache_ttl = min(1.0, window/10)`) prevents `_effective_burst` from reading the load sensor on every request. Without it, every call would do `zremrangebyscore + zcard` on the global sorted set — an O(log N) operation on a set that grows with aggregate traffic. With the cache, that scan happens at most once per TTL period (typically 0.1–1.0 seconds).

---

## `MemoryBackend` internals

### Sharded locking

```python
_NUM_SHARDS = 16

def _shard(self, key: str) -> int:
    return hash(key) % _NUM_SHARDS
```

Each shard owns:
- One `threading.Lock`
- One `dict[str, _Entry]` (KV store)
- One `defaultdict(list)` (sorted sets)
- One `defaultdict(dict)` (member→score index for O(1) zadd dedup)

Operations on key `"user:42"` always land in the same shard. Operations on `"user:43"` likely land in a different shard and proceed concurrently. Under a workload with many distinct keys (the normal rate-limiting case), most operations are uncontested.

`close()` acquires all 16 locks in index order before clearing, preventing deadlocks.

### Sorted-set operations

The sorted set is a Python `list` of `(score, member)` tuples, kept sorted at all times.

```
zadd:              bisect.insort()           O(log N) find + O(N) shift
zremrangebyscore:  bisect_left / bisect_right, del slice   O(log N) + O(K) where K=removed
zrange_by_score:   bisect_left / bisect_right, slice       O(log N) + O(K)
```

The `del zset[lo:hi]` slice deletion is O(N) in the worst case (shifting remaining elements), but K (the number of removed entries) is typically small in rate-limiting use (the pruning window is the last `window` seconds of requests, and most are recent). In practice the pruning operation is fast.

The member→score dict (`_zmembers`) enables O(1) lookup in `zadd` to detect and remove an existing entry before inserting the new one. Without it, `zadd` would need an O(N) linear scan.

---

## `RateLimitResult`

```python
@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    key: str
    limit: int
    remaining: int
    reset_after: float
    retry_after: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def headers(self) -> dict[str, str]: ...
```

Frozen dataclass — immutable after construction. This matters because `result` objects pass through decorators, middleware, and user code; mutable results would invite the class of bug where headers get modified downstream.

The `metadata` dict carries algorithm-specific debug info (`tokens`, `effective_burst`, `sw_count`, `layer`, etc.). It is intentionally untyped — algorithms evolve their diagnostics without changing the base interface.

---

## Configuration and extensibility

### `ConfigProvider` protocol

```python
class ConfigProvider(Protocol):
    def get_limit(self) -> int: ...
    def get_window(self) -> float: ...
```

Any object with these two methods works. `DynamicConfig` is the built-in implementation, but a feature-flag client, a database reader, or a Redis-backed config can implement this protocol without subclassing anything.

### Custom backends

Subclass `BaseBackend` and implement the 9 abstract methods. The sorted-set methods can be left as `raise NotImplementedError` if you only plan to use O(1) algorithms (FixedWindow, Counter, Token, Leaky), since those don't call sorted-set operations.

### Custom metrics

Subclass `MetricsCollector` and implement `record(key, result)`. Wrap any limiter with `ObservableRateLimiter(limiter, your_collector)`.

---

## Performance bottlenecks

**CPU-bound (MemoryBackend)**
The hot path for FixedWindow is: hash key → acquire shard lock → dict lookup → dict write → release lock. At ~120k ops/sec in a single thread, this is limited by Python object creation and GIL overhead, not lock contention. Adding threads beyond the GIL-release boundaries does not improve throughput for pure Python operations.

**I/O-bound (Redis, SQLite)**
Redis backend is network-latency-bound. SQLite is disk/WAL-bound. For sorted-set algorithms (SlidingWindow, Adaptive) on SQLite, each `is_allowed()` call executes 3–5 SQL statements. SQLite's WAL allows concurrent reads but serializes writes. This is why SlidingWindow drops to 276 ops/sec on SQLite.

**Sorted-set growth**
With an unlimited rate limiter and window=60s, the sorted set for a single key grows to `limit` entries. At `limit=10,000`, each `zadd` appends to a 10,000-element list (O(log N) find, O(N) shift). This is the primary reason `SlidingWindowRateLimiter` is slower than O(1) algorithms even in memory. In production, set `limit` to a realistic value — the sorted set stays bounded at `limit` entries due to the TTL-based pruning.

---

## What the test suite covers

```
tests/
├── conftest.py                        — Memory + SQLite fixtures
├── test_algorithms/
│   ├── test_fixed_window.py           — basics, window crossing, cost, validation
│   ├── test_sliding_window.py         — basics, expiry, cost
│   ├── test_token_bucket.py           — burst, refill, custom refill rate, cost
│   ├── test_leaky_bucket.py           — capacity, drain, cost
│   └── test_adaptive.py              — burst, load sensing, sliding window guard
├── test_backends/
│   └── test_backends.py              — conformance suite (Memory + SQLite + Redis/fakeredis)
├── test_decorators.py                 — @rate_limit, RateLimitContext, exceptions
├── test_async.py                      — AsyncRateLimiter, async_rate_limit
├── test_middleware.py                 — WSGI + ASGI (raw protocol, no framework)
├── test_improvements.py               — DynamicConfig, key_funcs, metrics, SWCounter
└── test_property.py                   — Hypothesis invariants across all algorithms
```

**Not tested (known gaps):**
- `AsyncRedisBackend` — requires a running Redis or async fakeredis; deferred.
- `examples/` — integration tests, not unit tests; manual verification only.
- Concurrent algorithm correctness under contention — the thread-safety tests in `TestMemoryBackend` check for exceptions, not for correctness of rate-limit semantics under race conditions.
