# smart-ratelimiter

[![PyPI version](https://img.shields.io/pypi/v/smart-ratelimiter.svg)](https://pypi.org/project/smart-ratelimiter/)
[![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A flexible, production-ready API rate limiter with **five algorithms**, **three pluggable backends**, and first-class support for decorators, WSGI, and ASGI middleware.

---

## Features

- **Five algorithms** — Fixed Window, Sliding Window, Token Bucket, Leaky Bucket, and an **Adaptive Hybrid** that automatically tightens under load
- **Three backends** — In-memory (default), Redis (distributed), SQLite (single-host multi-process)
- **Pluggable architecture** — swap any backend behind any algorithm with zero code changes
- **Decorator API** — one line to protect any function
- **WSGI + ASGI middleware** — drop-in for Flask, Django, FastAPI, Starlette
- **Standard HTTP headers** — `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `Retry-After`
- **Typed throughout** — full `py.typed` marker, strict mypy clean
- **Zero required dependencies** — Redis support is an optional extra

---

## Installation

```bash
pip install smart-ratelimiter          # core (no extra deps)
pip install smart-ratelimiter[redis]   # adds Redis backend
pip install smart-ratelimiter[dev]     # adds pytest, mypy, ruff
```

---

## Quick Start

```python
from ratelimiter import AdaptiveRateLimiter, MemoryBackend, RateLimitExceeded

limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60)

result = limiter.is_allowed("user:42")
if result.allowed:
    print(f"{result.remaining} requests remaining")
else:
    print(f"Rate limited. Retry after {result.retry_after:.1f}s")
```

---

## Algorithms

### Fixed Window

Divides time into fixed buckets. Simple and cheap — one counter per key.

```python
from ratelimiter import FixedWindowRateLimiter, MemoryBackend

limiter = FixedWindowRateLimiter(MemoryBackend(), limit=100, window=60)
result = limiter.is_allowed("user:42")
```

**Pros:** Minimal memory, constant time. **Cons:** Boundary burst — a caller can make 2× the limit by straddling a window boundary.

---

### Sliding Window

Stores a timestamped log of every request. No boundary burst problem.

```python
from ratelimiter import SlidingWindowRateLimiter, MemoryBackend

limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=100, window=60)
result = limiter.is_allowed("192.168.1.1")
```

**Pros:** Most accurate; immune to boundary exploits. **Cons:** O(N) memory per key (N = limit).

---

### Token Bucket

A bucket fills with tokens at a constant rate. Requests consume tokens. Handles legitimate bursts gracefully.

```python
from ratelimiter import TokenBucketRateLimiter, MemoryBackend

# Bucket holds 200 tokens; refills at 50 tokens/s
limiter = TokenBucketRateLimiter(
    MemoryBackend(), limit=200, window=60, refill_rate=50
)
result = limiter.is_allowed("api_key:abc")
```

**Pros:** Excellent burst tolerance. **Cons:** No hard boundary protection.

---

### Leaky Bucket

Requests fill a bucket that drains at a constant rate. Enforces a perfectly smooth output rate.

```python
from ratelimiter import LeakyBucketRateLimiter, MemoryBackend

limiter = LeakyBucketRateLimiter(MemoryBackend(), limit=100, window=10)
result = limiter.is_allowed("user:7")
```

**Pros:** Strictly smooth throughput. **Cons:** No burst allowance; can feel harsh under legitimate spikes.

---

### Adaptive Hybrid

Combines sliding window accuracy with token bucket burst tolerance, then adds a load-sensing layer that automatically tightens limits under high traffic and relaxes them under low traffic.

```python
from ratelimiter import AdaptiveRateLimiter, MemoryBackend

limiter = AdaptiveRateLimiter(
    backend=MemoryBackend(),
    limit=100,           # hard ceiling per window
    window=60.0,         # 100 req / 60 s sustained rate
    burst_multiplier=3,  # allow up to 300 burst tokens when quiet
    adaptive_window=300, # measure load over 5 minutes
    high_load_threshold=0.8,  # tighten above 80% of base rate
    penalty=0.5,         # cut burst cap by 50% under high load
)
result = limiter.is_allowed("tenant:acme")
print(result.metadata)
# {'layer': 'token_bucket', 'tokens': 299.0, 'effective_burst': 300, ...}
```

**How it works:**

1. **Sliding window guard** — prevents boundary exploitation; ceiling = current burst cap
2. **Token bucket** — refills at `limit / window` tokens/s; capacity adapts to load
3. **Load sensor** — tracks request rate over `adaptive_window`; when load is high, the burst cap shrinks; when load is low, full burst cap is restored

**Pros:** Best of all worlds — accurate, burst-tolerant, self-tuning. **Cons:** Slightly higher per-request overhead than simpler algorithms.

---

### Algorithm Comparison

| Algorithm       | Burst Support | Boundary Safe | Memory Use | Extra State |
|-----------------|:---:|:---:|:---:|:---:|
| Fixed Window    | ❌  | ❌  | O(1) | counter |
| Sliding Window  | ❌  | ✅  | O(N) | sorted set |
| Token Bucket    | ✅  | ❌  | O(1) | float + timestamp |
| Leaky Bucket    | ❌  | ✅  | O(1) | float + timestamp |
| **Adaptive**    | ✅  | ✅  | O(N) | sorted set + token state |

---

## Backends

### In-Memory (default)

Thread-safe. State is lost on restart. Best for single-process apps and testing.

```python
from ratelimiter.backends.memory import MemoryBackend
backend = MemoryBackend()
```

### Redis (distributed)

Requires `pip install smart-ratelimiter[redis]`. Best for multi-process / multi-host deployments.

```python
import redis
from ratelimiter.backends.redis_backend import RedisBackend

client = redis.Redis(host="localhost", port=6379, decode_responses=True)
backend = RedisBackend(client=client, key_prefix="myapp:")
```

### SQLite (single-host multi-process)

Zero extra dependencies. Persists across restarts. Best for single-host apps that need persistence.

```python
from ratelimiter.backends.sqlite_backend import SQLiteBackend

backend = SQLiteBackend(db_path="/var/lib/myapp/ratelimiter.db")
```

---

## Decorator API

```python
from ratelimiter import TokenBucketRateLimiter, MemoryBackend, rate_limit, RateLimitExceeded

limiter = TokenBucketRateLimiter(MemoryBackend(), limit=10, window=1)

# Static key — shared across all callers
@rate_limit(limiter)
def send_email(to: str) -> None:
    ...

# Dynamic key — per-caller limit
@rate_limit(limiter, key_func=lambda user_id, **kw: f"user:{user_id}")
def get_profile(user_id: int) -> dict:
    ...

# Custom cost
@rate_limit(limiter, cost=5)
def expensive_operation() -> None:
    ...

# Return None instead of raising on limit
@rate_limit(limiter, raise_on_limit=False)
def best_effort() -> str | None:
    return "data"
```

---

## Context Manager

```python
from ratelimiter import RateLimitContext, RateLimitExceeded

with RateLimitContext(limiter, key=f"user:{user_id}"):
    do_work()
```

---

## WSGI Middleware

```python
from flask import Flask
from ratelimiter import SlidingWindowRateLimiter, MemoryBackend
from ratelimiter.middleware import RateLimitMiddleware

app = Flask(__name__)
limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=60, window=60)

# Rate-limit by IP address (default)
app.wsgi_app = RateLimitMiddleware(app.wsgi_app, limiter=limiter)

# Or by a custom key
app.wsgi_app = RateLimitMiddleware(
    app.wsgi_app,
    limiter=limiter,
    key_func=lambda env: env.get("HTTP_X_API_KEY", env.get("REMOTE_ADDR", "unknown")),
)
```

---

## ASGI Middleware

```python
from fastapi import FastAPI
from ratelimiter import AdaptiveRateLimiter
from ratelimiter.backends.redis_backend import RedisBackend
from ratelimiter.middleware import AsyncRateLimitMiddleware

app = FastAPI()
limiter = AdaptiveRateLimiter(RedisBackend(), limit=100, window=60)

app.add_middleware(AsyncRateLimitMiddleware, limiter=limiter)
```

---

## RateLimitResult

Every `is_allowed()` call returns a `RateLimitResult`:

```python
result = limiter.is_allowed("user:42")

result.allowed      # bool  — whether the request is permitted
result.key          # str   — the key that was checked
result.limit        # int   — configured limit
result.remaining    # int   — requests remaining in this window
result.reset_after  # float — seconds until window/quota resets
result.retry_after  # float — seconds to wait (0 if allowed)
result.metadata     # dict  — algorithm-specific info
result.headers      # dict  — ready-to-use HTTP headers
```

### HTTP Headers

```python
response.headers.update(result.headers)
# X-RateLimit-Limit: 100
# X-RateLimit-Remaining: 42
# X-RateLimit-Reset: 37
# Retry-After: 12.50  (only present when rejected)
```

---

## Custom Backend

Implement `BaseBackend` to add any storage system:

```python
from ratelimiter.backends.base import BaseBackend

class MyBackend(BaseBackend):
    def get(self, key): ...
    def set(self, key, value, ttl=None): ...
    def delete(self, key): ...
    def incr(self, key, amount=1): ...
    def expire(self, key, ttl): ...
    def zadd(self, key, score, member): ...
    def zremrangebyscore(self, key, min_score, max_score): ...
    def zcard(self, key): ...
    def zrange_by_score(self, key, min_score, max_score): ...
```

---

## Development

```bash
git clone https://github.com/himanshu9209/smart-ratelimiter
cd smart-ratelimiter
pip install -e ".[dev]"
pytest --cov=ratelimiter --cov-report=term-missing
mypy src/
ruff check src/
```

---

## License

MIT — see [LICENSE](LICENSE).
