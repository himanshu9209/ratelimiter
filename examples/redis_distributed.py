"""
Redis distributed example — smart-ratelimiter
===============================================

Shows how to use smart-ratelimiter in a multi-process / multi-host deployment
backed by Redis.  Every process shares the same rate-limit state through Redis,
so limits are enforced globally, not per-instance.

Architecture:
    [Process 1] ──┐
    [Process 2] ──┼──► RedisBackend ──► Redis server
    [Process N] ──┘         (shared state)

Requirements:
    pip install smart-ratelimiter[redis] fastapi uvicorn

Run (requires a running Redis at localhost:6379):
    uvicorn examples.redis_distributed:app --workers 4 --reload

Or test without Redis using fakeredis:
    FAKEREDIS=1 uvicorn examples.redis_distributed:app --reload
"""

from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from smart_ratelimiter import (
    AdaptiveRateLimiter,
    MemoryBackend,
    RateLimitResult,
    TokenBucketRateLimiter,
)
from smart_ratelimiter.middleware import AsyncRateLimitMiddleware

# ---------------------------------------------------------------------------
# Backend — real Redis or fakeredis for local testing
# ---------------------------------------------------------------------------

def _build_backend():
    """Return a RedisBackend (real or fake) based on environment."""
    if os.getenv("FAKEREDIS"):
        # Local dev / CI — no real Redis needed
        import fakeredis
        from smart_ratelimiter.backends.redis_backend import RedisBackend
        client = fakeredis.FakeRedis(decode_responses=True)
        print("[redis_distributed] Using fakeredis (no real Redis required)")
        return RedisBackend(client=client, key_prefix="demo:")

    import redis as redis_lib
    from smart_ratelimiter.backends.redis_backend import RedisBackend

    redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    client = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    try:
        client.ping()
        print(f"[redis_distributed] Connected to Redis at {redis_url}")
    except Exception as exc:
        print(f"[redis_distributed] WARNING: Redis unavailable ({exc}). Falling back to MemoryBackend.")
        return MemoryBackend()

    return RedisBackend(client=client, key_prefix="demo:")


backend = _build_backend()

# ---------------------------------------------------------------------------
# Limiters — all sharing the same Redis backend
# ---------------------------------------------------------------------------

# Global guard: 1,000 req/min per IP across all processes
global_limiter = AdaptiveRateLimiter(
    backend=backend,
    limit=1000,
    window=60.0,
    burst_multiplier=2,
    adaptive_window=300.0,
    key_prefix="global:",
)

# Per-user API limiter: adaptive, 60 req/min, burst up to 180
api_limiter = AdaptiveRateLimiter(
    backend=backend,
    limit=60,
    window=60.0,
    burst_multiplier=3,
    adaptive_window=300.0,
    high_load_threshold=0.8,
    low_load_threshold=0.4,
    penalty=0.5,
    key_prefix="api:",
)

# Expensive endpoint: strict token bucket, 5 req/min
expensive_limiter = TokenBucketRateLimiter(
    backend=backend,
    limit=5,
    window=60.0,
    key_prefix="exp:",
)

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="smart-ratelimiter Redis distributed demo",
    description="Multi-process rate limiting backed by shared Redis state.",
)

app.add_middleware(
    AsyncRateLimitMiddleware,
    limiter=global_limiter,
    key_func=lambda scope: (
        dict(scope.get("headers", [])).get(b"x-forwarded-for", b"").decode().split(",")[0].strip()
        or (scope.get("client") or ("unknown", 0))[0]
    ),
)


# ---------------------------------------------------------------------------
# Client key helpers
# ---------------------------------------------------------------------------

def get_client_key(
    request: Request,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    if x_api_key:
        return f"key:{x_api_key}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def inject_rl_headers(response: Response, result: RateLimitResult) -> None:
    for k, v in result.headers.items():
        response.headers[k] = v


# ---------------------------------------------------------------------------
# Reusable rate-limit dependency
# ---------------------------------------------------------------------------

class RateLimitDep:
    def __init__(self, limiter: AdaptiveRateLimiter | TokenBucketRateLimiter, cost: int = 1):
        self.limiter = limiter
        self.cost = cost

    def __call__(
        self,
        response: Response,
        client_key: Annotated[str, Depends(get_client_key)],
    ) -> RateLimitResult:
        result = self.limiter.is_allowed(client_key, self.cost)
        inject_rl_headers(response, result)
        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "retry_after": round(result.retry_after, 2),
                    "message": f"Too many requests. Retry after {result.retry_after:.1f}s.",
                },
                headers=result.headers,
            )
        return result


api_rate_limit = RateLimitDep(api_limiter)
expensive_rate_limit = RateLimitDep(expensive_limiter)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root(request: Request):
    return {
        "message": "smart-ratelimiter Redis distributed demo",
        "backend": type(backend).__name__,
        "client": request.client.host if request.client else "unknown",
    }


@app.get("/data")
async def get_data(
    rl: Annotated[RateLimitResult, Depends(api_rate_limit)],
):
    """
    API endpoint with adaptive rate limiting.

    Limit: 60 req/min per client, adaptive burst up to 180 under low load.
    State is shared across ALL running processes via Redis.
    """
    return {
        "data": {"example": True},
        "rate_limit": {
            "remaining": rl.remaining,
            "reset_after": round(rl.reset_after, 2),
            "effective_burst": rl.metadata.get("effective_burst"),
        },
    }


@app.post("/compute")
async def run_compute(
    payload: dict,
    rl: Annotated[RateLimitResult, Depends(expensive_rate_limit)],
):
    """
    Expensive endpoint with strict token bucket.

    Limit: 5 req/min per client. No burst. State shared across processes.
    """
    return {
        "result": "computed",
        "tokens_remaining": rl.remaining,
    }


@app.get("/health")
async def health():
    """Health check — excluded from rate limiting."""
    try:
        ok = backend.ping()
    except Exception:
        ok = False
    return {"status": "ok", "backend_reachable": ok}


# ---------------------------------------------------------------------------
# Demo: show that state IS shared across invocations
# ---------------------------------------------------------------------------

@app.get("/demo/consume/{n}")
async def demo_consume(
    n: int,
    client_key: Annotated[str, Depends(get_client_key)],
):
    """
    Fire n requests against the api_limiter and report how many were allowed.
    Useful for demonstrating that Redis state persists across requests.
    """
    n = min(n, 200)  # safety cap
    allowed = 0
    denied = 0
    for _ in range(n):
        r = api_limiter.is_allowed(client_key)
        if r.allowed:
            allowed += 1
        else:
            denied += 1

    return {
        "requested": n,
        "allowed": allowed,
        "denied": denied,
        "client_key": client_key,
        "note": "State is shared — run this from two processes to see global enforcement.",
    }
