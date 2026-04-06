# smart-ratelimiter

<p align="center">
  <a href="https://pypi.org/project/smart-ratelimiter/"><img alt="PyPI" src="https://img.shields.io/pypi/v/smart-ratelimiter.svg?color=blue"></a>
  <a href="https://pypi.org/project/smart-ratelimiter/"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue"></a>
  <a href="https://opensource.org/licenses/MIT"><img alt="License" src="https://img.shields.io/badge/License-MIT-green.svg"></a>
 <a href="https://github.com/himanshu9209/smart-ratelimiter/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/himanshu9209/smart-ratelimiter/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-zero-brightgreen">
  <img alt="Typed" src="https://img.shields.io/badge/types-mypy%20strict-blue">
</p>

<p align="center">
  <b>Production-ready rate limiting for Python — six algorithms, three backends, one consistent API.</b><br>
  Works as a decorator, context manager, WSGI middleware, or ASGI middleware.<br>
  Zero required dependencies. Full type annotations. Redis optional.
</p>

---

## Why smart-ratelimiter?

Most rate-limiting libraries give you one algorithm and one backend. **smart-ratelimiter** gives you six algorithms to choose from (including an adaptive hybrid that auto-tunes itself), three pluggable backends, and a uniform API that works everywhere — from a simple `@rate_limit` decorator to production FastAPI or Flask middleware.

- **Pick the right algorithm** — not just the one the library happened to implement
- **Swap backends without touching your logic** — in-memory for dev, Redis for prod
- **Observe what's happening** — per-key metrics track allowed vs. dropped requests in real time
- **Change limits at runtime** — no restart needed thanks to `DynamicConfig`
- **Identify clients precisely** — built-in helpers for `X-Forwarded-For`, API keys, and composite keys

---

## Table of Contents

- [Installation](#installation)
- [Quick Start](#quick-start)
- [Algorithms](#algorithms)
  - [Which algorithm should I use?](#which-algorithm-should-i-use)
  - [Fixed Window](#fixed-window)
  - [Sliding Window Log](#sliding-window-log)
  - [Sliding Window Counter](#sliding-window-counter)
  - [Token Bucket](#token-bucket)
  - [Leaky Bucket](#leaky-bucket)
  - [Adaptive Hybrid](#adaptive-hybrid)
  - [Comparison table](#algorithm-comparison)
- [Backends](#backends)
- [Decorator API](#decorator-api)
- [Context Manager](#context-manager)
- [Middleware](#middleware)
  - [WSGI (Flask / Django)](#wsgi-middleware)
  - [ASGI (FastAPI / Starlette)](#asgi-middleware)
- [Client Identification](#client-identification)
- [Dynamic Configuration](#dynamic-configuration)
- [Observability & Metrics](#observability--metrics)
- [RateLimitResult reference](#ratelimitresult-reference)
- [Custom Backends](#custom-backends)
- [Development](#development)

---

## Installation

```bash
# Core — no extra dependencies
pip install smart-ratelimiter

# With Redis backend
pip install smart-ratelimiter[redis]

# Development tools (pytest, mypy, ruff, fakeredis)
pip install smart-ratelimiter[dev]
```

---

## Quick Start

```python
from ratelimiter import SlidingWindowRateLimiter, MemoryBackend

limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=100, window=60)

result = limiter.is_allowed("user:42")
if result.allowed:
    print(f"{result.remaining} requests left this minute")
else:
    print(f"Rate limited — retry in {result.retry_after:.1f}s")
```

Or protect any function with one decorator:

```python
from ratelimiter import TokenBucketRateLimiter, MemoryBackend, rate_limit

limiter = TokenBucketRateLimiter(MemoryBackend(), limit=10, window=1)

@rate_limit(limiter, key_func=lambda user_id, **_: f"user:{user_id}")
def send_email(user_id: int, to: str) -> None:
    ...  # called at most 10 times/s per user
```

---

## Algorithms

### Which algorithm should I use?

```
Need to allow short bursts?
├── Yes → Is memory per key a concern?
│         ├── No  → Token Bucket  (accurate, elegant burst handling)
│         └── Yes → Sliding Window Counter  (O(1) memory, ~99% accurate)
└── No  → Need perfectly smooth output?
          ├── Yes → Leaky Bucket  (constant drip, no bursts at all)
          └── No  → Need boundary-burst protection?
                    ├── Yes → Sliding Window Log  (exact, higher memory)
                    └── No  → Fixed Window  (cheapest, simplest)

High-traffic multi-tenant service with unpredictable load?
    → Adaptive Hybrid  (auto-tightens under load, relaxes when quiet)
```

---

### Fixed Window

Divides time into fixed, non-overlapping buckets. One `INCR` per request — the cheapest algorithm available.

```python
from ratelimiter import FixedWindowRateLimiter, MemoryBackend

limiter = FixedWindowRateLimiter(MemoryBackend(), limit=100, window=60)
result = limiter.is_allowed("user:42")
```

> **Trade-off:** A client can exploit the boundary to fire 2× the limit by sending `limit` requests just before the window rolls and `limit` more immediately after.

---

### Sliding Window Log

Stores a timestamped log of every request in the window. No boundary burst possible.

```python
from ratelimiter import SlidingWindowRateLimiter, MemoryBackend

limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=100, window=60)
result = limiter.is_allowed("192.168.1.1")
```

> **Trade-off:** O(N) memory per key (N = limit). Best when you need exact counts and can afford the storage.

---

### Sliding Window Counter

A memory-efficient alternative to the log. Blends two adjacent fixed-window counters using a weighted approximation — O(1) memory, ~98–99% accuracy, no boundary burst.

```python
from ratelimiter import SlidingWindowCounterRateLimiter, MemoryBackend

limiter = SlidingWindowCounterRateLimiter(MemoryBackend(), limit=100, window=60)
result = limiter.is_allowed("user:42")

# Extra metadata for observability
print(result.metadata)
# {'curr_count': 12, 'prev_count': 45, 'effective_count': 34.5, 'weight_prev': 0.5}
```

> **Trade-off:** Slightly approximate (bounded error) but uses constant memory regardless of traffic volume. Ideal for high-traffic services where the log's O(N) cost is prohibitive.

---

### Token Bucket

A bucket holds up to `limit` tokens that refill at a constant rate. Bursts are absorbed up to the bucket capacity; sustained rate is enforced by the refill speed.

```python
from ratelimiter import TokenBucketRateLimiter, MemoryBackend

# Bucket holds 200 tokens; refills at 50 tokens/s independent of window
limiter = TokenBucketRateLimiter(
    MemoryBackend(), limit=200, window=60, refill_rate=50
)
result = limiter.is_allowed("api_key:abc")
```

> **Trade-off:** Excellent burst handling, but no hard boundary protection — a persistent attacker at exactly the refill rate is never rejected.

---

### Leaky Bucket

Requests fill a bucket that drains at a constant leak rate. Enforces a perfectly smooth throughput regardless of incoming burst shape.

```python
from ratelimiter import LeakyBucketRateLimiter, MemoryBackend

limiter = LeakyBucketRateLimiter(MemoryBackend(), limit=100, window=10)
result = limiter.is_allowed("user:7")
```

> **Trade-off:** Zero burst tolerance once the bucket is full. Good for protecting downstream systems that can't handle spikes at all.

---

### Adaptive Hybrid

Combines sliding window accuracy with token bucket burst tolerance, plus a **load-sensing layer** that automatically tightens the burst cap under high traffic and restores it when traffic drops — with no manual tuning.

```python
from ratelimiter import AdaptiveRateLimiter, MemoryBackend

limiter = AdaptiveRateLimiter(
    backend=MemoryBackend(),
    limit=100,                # hard ceiling: 100 req per window
    window=60.0,              # base window in seconds
    burst_multiplier=3,       # up to 300 burst tokens when quiet
    adaptive_window=300,      # measure load over the last 5 minutes
    high_load_threshold=0.8,  # tighten when traffic > 80% of base rate
    low_load_threshold=0.4,   # relax when traffic < 40% of base rate
    penalty=0.5,              # cut burst cap by 50% under high load
)
result = limiter.is_allowed("tenant:acme")
print(result.metadata)
# {'layer': 'token_bucket', 'tokens': 299.0, 'effective_burst': 300,
#  'refill_rate': 5.0, 'sw_count': 1}
```

**How it works:**

| Layer | Role |
|-------|------|
| Sliding window guard | Hard ceiling at the current burst cap; prevents boundary exploitation |
| Token bucket | Refills at `limit / window` tokens/s; enforces sustained average rate |
| Load sensor | Tracks request rate over `adaptive_window`; shrinks burst cap under load, restores it when quiet |

> **Best for:** Multi-tenant APIs, public endpoints, or any service where traffic is unpredictable and you want automatic protection without manual tuning.

---

### Algorithm Comparison

| Algorithm | Burst Support | Boundary Safe | Memory | State |
|-----------|:---:|:---:|:---:|---|
| Fixed Window | ❌ | ❌ | O(1) | 1 counter |
| Sliding Window Log | ❌ | ✅ | O(N) | sorted timestamp set |
| **Sliding Window Counter** | ❌ | ✅ | **O(1)** | 2 counters |
| Token Bucket | ✅ | ❌ | O(1) | float + timestamp |
| Leaky Bucket | ❌ | ✅ | O(1) | float + timestamp |
| **Adaptive Hybrid** | ✅ | ✅ | O(N) | sorted set + token state |

---

## Backends

All backends implement the same `BaseBackend` interface. Swap one for another with a single line change.

### In-Memory

Thread-safe. State lives in-process and is lost on restart. Perfect for single-process apps and testing.

```python
from ratelimiter import MemoryBackend

backend = MemoryBackend()
```

### Redis (distributed)

Requires `pip install smart-ratelimiter[redis]`. Share rate-limit state across multiple processes or hosts.

```python
import redis
from ratelimiter.backends.redis_backend import RedisBackend

client = redis.Redis(host="localhost", port=6379, decode_responses=True)
backend = RedisBackend(client=client, key_prefix="myapp:")
```

### SQLite (persistent, single-host)

Zero extra dependencies. Persists across restarts. Uses WAL mode for safe concurrent access.

```python
from ratelimiter import SQLiteBackend

backend = SQLiteBackend(db_path="/var/lib/myapp/ratelimiter.db")
```

---

## Decorator API

```python
from ratelimiter import TokenBucketRateLimiter, MemoryBackend, rate_limit

limiter = TokenBucketRateLimiter(MemoryBackend(), limit=10, window=1)

# Shared key across all callers
@rate_limit(limiter)
def send_notification() -> None: ...

# Per-caller key derived from arguments
@rate_limit(limiter, key_func=lambda user_id, **_: f"user:{user_id}")
def get_profile(user_id: int) -> dict: ...

# Request costs more than 1 token (e.g. bulk operations)
@rate_limit(limiter, cost=5)
def bulk_export() -> None: ...

# Return None on limit instead of raising
@rate_limit(limiter, raise_on_limit=False)
def best_effort() -> str | None:
    return "data"
```

When `raise_on_limit=True` (default), `RateLimitExceeded` is raised:

```python
from ratelimiter import RateLimitExceeded

try:
    get_profile(user_id=42)
except RateLimitExceeded as exc:
    print(f"Retry in {exc.retry_after:.1f}s")
```

---

## Context Manager

```python
from ratelimiter import RateLimitContext, RateLimitExceeded

with RateLimitContext(limiter, key=f"user:{user_id}"):
    do_work()   # RateLimitExceeded raised on __enter__ if over limit
```

---

## Middleware

### WSGI Middleware

Drop-in for Flask, Django, or any PEP 3333 application.

```python
from flask import Flask
from ratelimiter import SlidingWindowRateLimiter, MemoryBackend
from ratelimiter.middleware import RateLimitMiddleware
from ratelimiter.key_funcs import wsgi_api_key_func

app = Flask(__name__)
limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=60, window=60)

app.wsgi_app = RateLimitMiddleware(
    app.wsgi_app,
    limiter=limiter,
    key_func=wsgi_api_key_func("X-API-Key"),  # API key, fallback to IP
)
```

Rejected requests receive HTTP 429 with `Retry-After` header. Allowed requests get `X-RateLimit-*` headers automatically injected.

### ASGI Middleware

Drop-in for FastAPI, Starlette, or any ASGI application.

```python
from fastapi import FastAPI
from ratelimiter import AdaptiveRateLimiter, MemoryBackend
from ratelimiter.middleware import AsyncRateLimitMiddleware
from ratelimiter.key_funcs import asgi_api_key_func

app = FastAPI()
limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60)

app.add_middleware(
    AsyncRateLimitMiddleware,
    limiter=limiter,
    key_func=asgi_api_key_func("X-API-Key"),
)
```

See [`examples/flask_example.py`](examples/flask_example.py) and [`examples/fastapi_example.py`](examples/fastapi_example.py) for complete integration patterns including per-endpoint limits and dependency injection.

---

## Client Identification

Built-in helpers make it easy to identify clients by IP or API key — for both WSGI and ASGI middleware.

```python
from ratelimiter.key_funcs import (
    wsgi_ip_func, wsgi_api_key_func, wsgi_composite_key_func,
    asgi_ip_func, asgi_api_key_func, asgi_composite_key_func,
)

# Client IP — honours X-Forwarded-For when behind a proxy
key_func = wsgi_ip_func(trust_x_forwarded_for=True)

# API key header, falls back to IP if header is absent
key_func = wsgi_api_key_func("X-API-Key")

# Combine multiple signals into a composite key
key_func = wsgi_composite_key_func(
    wsgi_ip_func(),
    wsgi_api_key_func("X-API-Key"),
    separator="|",
)

# Same helpers available for ASGI scopes
key_func = asgi_api_key_func("Authorization")
```

> **Security note:** Only trust `X-Forwarded-For` when your proxy strips or overwrites it — otherwise clients can spoof their IP.

---

## Dynamic Configuration

Change rate limits at runtime without restarting your service.

```python
from ratelimiter import DynamicConfig, FixedWindowRateLimiter, MemoryBackend

# Create a shared config object
cfg = DynamicConfig(limit=100, window=60)

# Attach it to one or more limiters
limiter = FixedWindowRateLimiter(
    MemoryBackend(), limit=100, window=60, config_provider=cfg
)

# Later — from an admin endpoint, config reload, feature flag, etc.
cfg.update(limit=50)   # effective immediately on the next is_allowed() call
cfg.update(window=30)  # or update both at once: cfg.update(limit=50, window=30)
```

`DynamicConfig` is thread-safe. Every algorithm (`FixedWindow`, `SlidingWindow`, `SlidingWindowCounter`, `TokenBucket`, `LeakyBucket`, `Adaptive`) accepts a `config_provider=` argument.

You can also implement the `ConfigProvider` protocol yourself — any object with `get_limit() -> int` and `get_window() -> float` qualifies:

```python
from ratelimiter.config import ConfigProvider

class FeatureFlagConfig:
    """Pull limits from your feature-flag service."""

    def get_limit(self) -> int:
        return feature_flags.get("api_rate_limit", default=100)

    def get_window(self) -> float:
        return 60.0
```

---

## Observability & Metrics

Track how many requests are allowed and dropped per client — essential for SRE work and DoS detection.

```python
from ratelimiter import (
    SlidingWindowRateLimiter, MemoryBackend,
    InMemoryMetricsCollector, ObservableRateLimiter,
)

metrics = InMemoryMetricsCollector()

limiter = ObservableRateLimiter(
    SlidingWindowRateLimiter(MemoryBackend(), limit=10, window=60),
    metrics,
)

# Use limiter normally
for _ in range(15):
    limiter.is_allowed("user:42")

# Inspect per-key stats
print(metrics.get_stats("user:42"))
# {'key': 'user:42', 'allowed': 10, 'dropped': 5, 'total': 15, 'drop_rate': 0.333}

# Or global stats across all keys
print(metrics.get_stats())
# {'allowed': 10, 'dropped': 5, 'total': 15, 'drop_rate': 0.333, 'per_key': {...}}
```

`ObservableRateLimiter` is a **non-intrusive wrapper** — it does not modify the underlying algorithm and adds negligible overhead.

**Push to Prometheus, StatsD, or any backend** by subclassing `MetricsCollector`:

```python
from ratelimiter.metrics import MetricsCollector
from ratelimiter.algorithms.base import RateLimitResult

class PrometheusCollector(MetricsCollector):
    def record(self, key: str, result: RateLimitResult) -> None:
        if result.allowed:
            REQUESTS_ALLOWED.labels(key=key).inc()
        else:
            REQUESTS_DROPPED.labels(key=key).inc()
```

---

## RateLimitResult Reference

Every `is_allowed()` call returns a `RateLimitResult`:

```python
result = limiter.is_allowed("user:42", cost=1)

result.allowed      # bool  — True if the request is permitted
result.key          # str   — the key that was checked
result.limit        # int   — the configured limit
result.remaining    # int   — requests remaining in this window
result.reset_after  # float — seconds until the window / quota resets
result.retry_after  # float — seconds to wait before retrying (0 if allowed)
result.metadata     # dict  — algorithm-specific data (token count, bucket level, …)
result.headers      # dict  — ready-to-use HTTP response headers
```

### HTTP Headers

`result.headers` returns a dict you can inject directly into any HTTP response:

```python
response.headers.update(result.headers)
```

```
X-RateLimit-Limit:     100
X-RateLimit-Remaining: 42
X-RateLimit-Reset:     37
Retry-After:           12.50   ← only present when the request is rejected
```

---

## Custom Backends

Implement `BaseBackend` to connect any storage system — DynamoDB, Memcached, Cassandra, etc.:

```python
from ratelimiter.backends.base import BaseBackend
from typing import Any, Optional

class MyBackend(BaseBackend):
    def get(self, key: str) -> Optional[Any]: ...
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr(self, key: str, amount: int = 1) -> int: ...
    def expire(self, key: str, ttl: float) -> None: ...

    # Sorted-set operations (used by Sliding Window Log and Adaptive)
    def zadd(self, key: str, score: float, member: str) -> None: ...
    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int: ...
    def zcard(self, key: str) -> int: ...
    def zrange_by_score(self, key: str, min_score: float, max_score: float) -> list: ...
```

Once implemented, use it with any algorithm:

```python
backend = MyBackend()
limiter = AdaptiveRateLimiter(backend, limit=100, window=60)
```

---

## Development

```bash
git clone https://github.com/himanshu9209/smart-ratelimiter
cd smart-ratelimiter
pip install -e ".[dev]"

# Run tests
pytest --cov=ratelimiter --cov-report=term-missing

# Type check
mypy src/

# Lint
ruff check src/
```

The test suite covers all six algorithms, all three backends, middleware (WSGI + ASGI), decorators, dynamic configuration, metrics collection, and client identification helpers.

---

## License

[MIT](LICENSE) — free to use in commercial and open-source projects.
