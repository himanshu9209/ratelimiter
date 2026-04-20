# smart-ratelimiter

<p align="center">
  <a href="https://pypi.org/project/smart-ratelimiter/"><img alt="PyPI" src="https://img.shields.io/pypi/v/smart-ratelimiter.svg?color=blue"></a>
  <a href="https://pypi.org/project/smart-ratelimiter/"><img alt="Python" src="https://img.shields.io/badge/python-3.9%20%7C%203.10%20%7C%203.11%20%7C%203.12-blue"></a>
  <a href="https://opensource.org/licenses/MIT"><img alt="License" src="https://img.shields.io/badge/License-MIT-green.svg"></a>
  <a href="https://github.com/himanshu9209/ratelimiter/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/himanshu9209/ratelimiter/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Zero dependencies" src="https://img.shields.io/badge/dependencies-zero-brightgreen">
  <img alt="Typed" src="https://img.shields.io/badge/types-mypy%20strict-blue">
</p>

<p align="center">
  <b>The only Python rate limiter that tightens itself under load — and relaxes when traffic drops.</b><br><br>
  A three-layer hybrid algorithm (sliding window + token bucket + global load sensor)<br>
  that adapts its burst cap in real time without any config change or restart.<br><br>
  Zero dependencies &nbsp;·&nbsp; Six algorithms &nbsp;·&nbsp; Three backends &nbsp;·&nbsp; One consistent API
</p>

---

## The problem no static rate limiter solves

Every rate limiter lets you set a number. The problem: **the right number under normal traffic is the wrong number during a spike.**

```
Normal traffic:   50 req/s  →  limit=100/min feels generous, users happy
Traffic spike:   500 req/s  →  limit=100/min gets overwhelmed, OR
                              you set it so low that legitimate traffic suffers
```

You cannot tune your way out of this. The spike you're protecting against and the normal load you want to permit are different situations — a static ceiling is a tradeoff, not a solution.

**`AdaptiveRateLimiter` solves this directly.** Under low load, it offers a generous burst allowance. Under high load, it tightens automatically. When traffic drops, it relaxes again. No intervention required.

---

## How AdaptiveRateLimiter actually works

Three layers run on every `is_allowed()` call:

```
Request arrives
      |
      v
+--------------------------------------------------+
|  Layer 0: Global Load Sensor                     |
|                                                  |
|  Reads a shared sorted set written to by ALL     |
|  callers on this limiter instance.               |
|  Computes: measured_rate = requests / adap_window|
|                                                  |
|  Low load  (<40% of base rate) -> full burst cap |
|  High load (>80% of base rate) -> burst cap x0.5 |
|  In between -> linearly interpolated             |
|                                                  |
|  Result cached for ~0.1-1s to avoid hot-path I/O |
+---------------------+----------------------------+
                      |  effective_burst computed
                      v
+--------------------------------------------------+
|  Layer 1: Sliding Window Hard Ceiling            |
|                                                  |
|  Counts exact requests in the last `window` sec. |
|  Rejects if: sw_count + cost > effective_burst   |
|                                                  |
|  Prevents boundary-burst exploitation that       |
|  breaks token bucket and fixed-window limiters.  |
+---------------------+----------------------------+
                      |  exact count verified
                      v
+--------------------------------------------------+
|  Layer 2: Token Bucket                           |
|                                                  |
|  Tokens refill at effective_burst / window per s |
|  Rejects if: tokens < cost                       |
|                                                  |
|  Enforces the sustained average rate and allows  |
|  smooth bursts up to the current burst cap.      |
+---------------------+----------------------------+
                      |  allowed
                      v
           Record in sliding window +
           Record in global load sensor
```

**Why three layers?**
- Token bucket alone: attackable at window boundaries, no system-level awareness
- Sliding window alone: no burst tolerance, binary accept/reject
- Load sensor alone: reacts to aggregate load but not to individual key abuse
- All three together: burst-tolerant, boundary-safe, and system-aware

**The global load sensor is the novel part.** Every allowed request from every caller writes to the same sorted set. The burst cap is *global* — heavy traffic from one tenant tightens the burst cap for everyone on the same limiter. This is intentional: the goal is system stability, not per-tenant fairness. (For per-tenant isolation, create one `AdaptiveRateLimiter` per tenant.)

---

## Quick Start

```python
from smart_ratelimiter import AdaptiveRateLimiter, MemoryBackend

limiter = AdaptiveRateLimiter(
    backend=MemoryBackend(),
    limit=100,            # hard ceiling: 100 req per window
    window=60.0,          # base window in seconds
    burst_multiplier=3,   # up to 300 burst tokens when quiet
)

result = limiter.is_allowed("user:42")
if result.allowed:
    print(f"{result.remaining} requests left")
else:
    print(f"Rate limited — retry in {result.retry_after:.1f}s")
```

Protect any function with one decorator:

```python
from smart_ratelimiter import AdaptiveRateLimiter, MemoryBackend, rate_limit

limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60, burst_multiplier=3)

@rate_limit(limiter, key_func=lambda user_id, **_: f"user:{user_id}")
def call_llm_api(user_id: int, prompt: str) -> str:
    ...  # auto-throttles under load, opens up when quiet
```

---

## AdaptiveRateLimiter — full configuration

```python
from smart_ratelimiter import AdaptiveRateLimiter, MemoryBackend

limiter = AdaptiveRateLimiter(
    backend=MemoryBackend(),
    limit=100,                # hard ceiling per window
    window=60.0,              # base window in seconds
    burst_multiplier=3,       # burst cap = limit x multiplier when quiet
    adaptive_window=300,      # measure load over the last 5 minutes
    high_load_threshold=0.8,  # tighten when traffic > 80% of base rate
    low_load_threshold=0.4,   # relax when traffic < 40% of base rate
    penalty=0.5,              # shrink burst cap by 50% under high load
)

result = limiter.is_allowed("tenant:acme")

# Every result carries full diagnostic metadata
print(result.metadata)
# {
#   'layer': 'token_bucket',    <- which layer made the decision
#   'tokens': 299.0,            <- token bucket level right now
#   'effective_burst': 300,     <- current burst cap (drops under load)
#   'refill_rate': 5.0,         <- tokens/sec being restored
#   'sw_count': 1               <- exact requests in current window
# }
```

**What each parameter does:**

| Parameter | Default | Effect |
|---|---|---|
| `limit` | required | Hard ceiling that never moves — the absolute maximum per `window` |
| `window` | required | Time window in seconds |
| `burst_multiplier` | `2` | Burst cap = `limit × multiplier` during quiet periods |
| `adaptive_window` | `window × 5` | How far back to look when measuring load |
| `high_load_threshold` | `0.8` | Fraction of base rate at which burst cap starts shrinking |
| `low_load_threshold` | `0.4` | Fraction at which burst cap is fully restored |
| `penalty` | `0.5` | How much to cut the burst cap under high load (0 = no cut, 1 = full cut) |

---

## Real-world scenarios

### Multi-tenant API

```python
# One limiter for all tenants — global load sensor provides system-level protection
limiter = AdaptiveRateLimiter(MemoryBackend(), limit=1000, window=60, burst_multiplier=2)

# Each tenant gets its own sliding-window + token-bucket state
result = limiter.is_allowed(f"tenant:{tenant_id}")

# During a spike from ANY tenant, burst caps tighten for everyone
# -> system stays stable without per-tenant manual limits
```

### LLM / expensive downstream calls

```python
# Generous burst when the inference queue is short,
# aggressive throttling when it fills up
limiter = AdaptiveRateLimiter(
    MemoryBackend(),
    limit=60,           # 1 req/s average
    window=60,
    burst_multiplier=5, # allow up to 5 req/s burst when quiet
    penalty=0.8,        # cut to 1 req/s burst during high load
)
```

### Distributed (Redis) public endpoint

```python
import redis
from smart_ratelimiter import AdaptiveRateLimiter
from smart_ratelimiter.backends.redis_backend import RedisBackend

# Shared Redis -> rate limiting works across all app instances
client = redis.Redis(host="redis", decode_responses=True)
limiter = AdaptiveRateLimiter(
    RedisBackend(client, key_prefix="api:"),
    limit=100, window=60, burst_multiplier=3,
)
```

---

## Installation

```bash
# Core — no extra dependencies
pip install smart-ratelimiter

# With Redis backend
pip install smart-ratelimiter[redis]

# Development tools (pytest, mypy, ruff, fakeredis, hypothesis)
pip install smart-ratelimiter[dev]
```

---

## All six algorithms

### Which one should I use?

```
Is your traffic unpredictable, bursty, or multi-tenant?
    -> AdaptiveRateLimiter (*)  (auto-tightens under load, relaxes when quiet)

Do you need burst tolerance (handle spikes without immediate rejection)?
+-- Yes -> Is per-key memory a concern at high scale?
|         +-- No  -> TokenBucketRateLimiter          (cleanest burst handling)
|         +-- Yes -> SlidingWindowCounterRateLimiter  (O(1) memory, 99% accurate)
+-- No  -> Do you need perfectly smooth output (no bursts at all)?
          +-- Yes -> LeakyBucketRateLimiter           (constant drip rate)
          +-- No  -> Do you need exact counts with no boundary exploit?
                    +-- Yes -> SlidingWindowRateLimiter   (exact, higher memory)
                    +-- No  -> FixedWindowRateLimiter     (cheapest, simplest)
```

### Algorithm comparison

| Algorithm | Auto-tunes | Burst | Boundary-safe | Memory |
|---|:---:|:---:|:---:|:---:|
| **(*) AdaptiveRateLimiter** | ✅ | ✅ | ✅ | O(N) |
| FixedWindowRateLimiter | ❌ | ❌ | ❌ | O(1) |
| SlidingWindowRateLimiter | ❌ | ❌ | ✅ | O(N) |
| SlidingWindowCounterRateLimiter | ❌ | ❌ | ✅ | **O(1)** |
| TokenBucketRateLimiter | ❌ | ✅ | ❌ | O(1) |
| LeakyBucketRateLimiter | ❌ | ❌ | ✅ | O(1) |

---

## Backends

All backends implement the same interface. One line change to switch from dev to prod.

| Backend | Use case | Extra deps |
|---|---|---|
| `MemoryBackend` | Single-process, dev, testing | None |
| `SQLiteBackend` | Persistent, single-host | None |
| `RedisBackend` | Distributed, multi-host | `pip install smart-ratelimiter[redis]` |

```python
# Dev
from smart_ratelimiter import MemoryBackend
backend = MemoryBackend()

# Prod (same limiter code, different backend)
import redis
from smart_ratelimiter.backends.redis_backend import RedisBackend
backend = RedisBackend(redis.Redis(host="redis", decode_responses=True))
```

---

## Middleware

### WSGI (Flask / Django)

```python
from flask import Flask
from smart_ratelimiter import AdaptiveRateLimiter, MemoryBackend
from smart_ratelimiter.middleware import RateLimitMiddleware
from smart_ratelimiter.key_funcs import wsgi_api_key_func

app = Flask(__name__)
limiter = AdaptiveRateLimiter(MemoryBackend(), limit=60, window=60, burst_multiplier=3)

app.wsgi_app = RateLimitMiddleware(
    app.wsgi_app,
    limiter=limiter,
    key_func=wsgi_api_key_func("X-API-Key"),  # API key, fallback to IP
)
# Allowed:  injects X-RateLimit-Limit / Remaining / Reset headers
# Rejected: returns HTTP 429 with Retry-After header
```

### ASGI (FastAPI / Starlette)

```python
from fastapi import FastAPI
from smart_ratelimiter import AdaptiveRateLimiter, MemoryBackend
from smart_ratelimiter.middleware import AsyncRateLimitMiddleware
from smart_ratelimiter.key_funcs import asgi_api_key_func

app = FastAPI()
limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60, burst_multiplier=3)

app.add_middleware(
    AsyncRateLimitMiddleware,
    limiter=limiter,
    key_func=asgi_api_key_func("X-API-Key"),
)
```

See [`examples/fastapi_example.py`](examples/fastapi_example.py) and [`examples/flask_example.py`](examples/flask_example.py) for complete patterns including per-endpoint limits and dependency injection.

---

## Decorator API

```python
from smart_ratelimiter import TokenBucketRateLimiter, MemoryBackend, rate_limit

limiter = TokenBucketRateLimiter(MemoryBackend(), limit=10, window=1)

@rate_limit(limiter, key_func=lambda user_id, **_: f"user:{user_id}")
def get_profile(user_id: int) -> dict: ...

@rate_limit(limiter, cost=5)           # bulk ops cost more tokens
def bulk_export() -> None: ...

@rate_limit(limiter, raise_on_limit=False)   # return None instead of raising
def best_effort() -> str | None: ...
```

```python
from smart_ratelimiter import RateLimitExceeded

try:
    get_profile(user_id=42)
except RateLimitExceeded as exc:
    print(f"Retry in {exc.retry_after:.1f}s")
```

---

## Context Manager

```python
from smart_ratelimiter import RateLimitContext, RateLimitExceeded

with RateLimitContext(limiter, key=f"user:{user_id}"):
    do_work()  # RateLimitExceeded raised on __enter__ if over limit
```

---

## Dynamic Configuration

Change limits at runtime without restarting:

```python
from smart_ratelimiter import DynamicConfig, AdaptiveRateLimiter, MemoryBackend

cfg = DynamicConfig(limit=100, window=60)
limiter = AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60, config_provider=cfg)

# From an admin endpoint, feature flag, or config reload:
cfg.update(limit=50)   # effective on the next is_allowed() call
```

Implement the `ConfigProvider` protocol to pull limits from any source:

```python
from smart_ratelimiter.config import ConfigProvider

class FeatureFlagConfig:
    def get_limit(self) -> int:
        return feature_flags.get("api_rate_limit", default=100)

    def get_window(self) -> float:
        return 60.0
```

---

## Observability & Metrics

```python
from smart_ratelimiter import (
    AdaptiveRateLimiter, MemoryBackend,
    InMemoryMetricsCollector, ObservableRateLimiter,
)

metrics = InMemoryMetricsCollector()
limiter = ObservableRateLimiter(
    AdaptiveRateLimiter(MemoryBackend(), limit=100, window=60),
    metrics,
)

for _ in range(15):
    limiter.is_allowed("user:42")

print(metrics.get_stats("user:42"))
# {'key': 'user:42', 'allowed': 10, 'dropped': 5, 'total': 15, 'drop_rate': 0.333}
```

Push to Prometheus, StatsD, or any system by subclassing `MetricsCollector`:

```python
from smart_ratelimiter.metrics import MetricsCollector

class PrometheusCollector(MetricsCollector):
    def record(self, key: str, result) -> None:
        counter = REQUESTS_ALLOWED if result.allowed else REQUESTS_DROPPED
        counter.labels(key=key).inc()
```

---

## Client Identification

```python
from smart_ratelimiter.key_funcs import (
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
```

> **Security note:** Only trust `X-Forwarded-For` when your proxy strips or overwrites it — otherwise clients can spoof their IP.

---

## RateLimitResult reference

```python
result = limiter.is_allowed("user:42", cost=1)

result.allowed      # bool  — True if the request is permitted
result.remaining    # int   — requests remaining in this window
result.retry_after  # float — seconds to wait before retrying (0 if allowed)
result.reset_after  # float — seconds until quota resets
result.headers      # dict  — ready-to-use HTTP response headers
result.metadata     # dict  — algorithm diagnostics (tokens, layer, burst cap...)
```

`result.headers` is ready to inject into any HTTP response:

```
X-RateLimit-Limit:     100
X-RateLimit-Remaining: 42
X-RateLimit-Reset:     37
Retry-After:           12.50   <- only present when the request is rejected
```

---

## Custom Backends

Subclass `BaseBackend` to connect DynamoDB, Memcached, Cassandra, or anything else:

```python
from smart_ratelimiter.backends.base import BaseBackend
from typing import Any, Optional

class MyBackend(BaseBackend):
    def get(self, key: str) -> Optional[Any]: ...
    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None: ...
    def delete(self, key: str) -> None: ...
    def incr(self, key: str, amount: int = 1) -> int: ...
    def expire(self, key: str, ttl: float) -> None: ...

    # Only needed for SlidingWindow and Adaptive algorithms
    def zadd(self, key: str, score: float, member: str) -> None: ...
    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int: ...
    def zcard(self, key: str) -> int: ...
    def zrange_by_score(self, key: str, min_score: float, max_score: float) -> list: ...
```

---

## Benchmarks

5,000 iterations per algorithm, 500-iteration warmup, Python 3.12, effectively-unlimited limit so no requests are rejected.

### In-Memory Backend — single-threaded

| Algorithm | ops/sec | mean µs | p50 µs | p95 µs | p99 µs |
|---|---:|---:|---:|---:|---:|
| LeakyBucket | 249,237 | 3.85 | 3.80 | 4.00 | 4.30 |
| TokenBucket | 188,437 | 5.07 | 5.00 | 7.70 | 7.90 |
| SlidingWindowCounter | 129,097 | 7.50 | 6.90 | 11.90 | 17.00 |
| FixedWindow | 117,297 | 8.07 | 7.10 | 12.20 | 30.39 |
| **Adaptive** | **59,526** | **16.55** | **13.70** | **26.70** | **48.50** |
| SlidingWindow | 4,245 | 235.12 | 204.95 | 563.78 | 747.30 |

**Adaptive at 59k ops/sec is doing 3x the work** of a simple counter: one sorted-set prune + count (sliding window), one sorted-set read (load sensor), one token-bucket get/set — all in ~16µs mean. That's the cost of correctness and self-tuning.

### In-Memory Backend — concurrent (aggregate ops/sec across N threads)

| Algorithm | 1 thread | 2 threads | 4 threads | 8 threads |
|---|---:|---:|---:|---:|
| FixedWindow | ~117k | ~190k | ~220k | ~230k |
| **Adaptive** | **~60k** | **~95k** | **~110k** | **~115k** |
| SlidingWindow | ~4k | ~6k | ~7k | ~7k |

Scales near-linearly to 2 threads thanks to 16-shard locking in `MemoryBackend`. GIL caps further gains for pure-Python operations.

### SQLite Backend (WAL mode, in-memory)

| Algorithm | ops/sec | mean µs | p99 µs |
|---|---:|---:|---:|
| FixedWindow | 56,170 | 17.53 | 46.29 |
| SlidingWindowCounter | 31,958 | 30.99 | 79.19 |
| TokenBucket | 43,335 | 22.83 | 68.89 |
| LeakyBucket | 39,095 | 25.31 | 89.80 |
| Adaptive | 4,230 | 235.83 | 600.17 |

```bash
PYTHONPATH=src python benchmark.py
```

---

## Development

```bash
git clone https://github.com/himanshu9209/ratelimiter
cd ratelimiter
pip install -e ".[dev]"

pytest --cov=smart_ratelimiter --cov-report=term-missing   # 226 tests
mypy src/
ruff check src/
```

The test suite covers all six algorithms, all three backends (including Redis via fakeredis), middleware (WSGI + ASGI), decorators, dynamic configuration, metrics, client identification helpers, and property-based invariant tests via Hypothesis.

---

https://dev.to/himanshu_patel_56287109b6/python-rate-limiter-that-tunes-itself-heres-why-that-matters-25ig

## License

[MIT](LICENSE) — free to use in commercial and open-source projects.
