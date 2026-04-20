"""
FastAPI example — smart-ratelimiter
=====================================

Demonstrates:

1. ASGI middleware for global IP-based rate limiting
2. Dependency injection pattern (per-endpoint, per-user limits)
3. Redis backend for distributed deployments
4. Adaptive limiter with load-aware burst control

Run:
    pip install fastapi uvicorn smart-ratelimiter
    uvicorn examples.fastapi_example:app --reload
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from smart_ratelimiter import (
    AdaptiveRateLimiter,
    MemoryBackend,
    RateLimitExceeded,
    RateLimitResult,
    SlidingWindowRateLimiter,
    TokenBucketRateLimiter,
)
from smart_ratelimiter.middleware import AsyncRateLimitMiddleware

app = FastAPI(title="smart-ratelimiter FastAPI demo")

# ---------------------------------------------------------------------------
# Limiters
# ---------------------------------------------------------------------------

# Global: 500 req/min per IP — enforced by ASGI middleware
global_limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=500, window=60)

# User-scoped API limiter: adaptive, 60 req/min sustained, burst up to 180
api_limiter = AdaptiveRateLimiter(
    MemoryBackend(),
    limit=60,
    window=60,
    burst_multiplier=3,
    adaptive_window=300,
)

# Expensive endpoint: strict token bucket, 10 req/min, no burst
inference_limiter = TokenBucketRateLimiter(MemoryBackend(), limit=10, window=60)


# ---------------------------------------------------------------------------
# ASGI middleware — global guard
# ---------------------------------------------------------------------------

app.add_middleware(
    AsyncRateLimitMiddleware,
    limiter=global_limiter,
    key_func=lambda scope: (
        dict(scope.get("headers", [])).get(b"x-forwarded-for", b"").decode().split(",")[0].strip()
        or (scope.get("client") or ("unknown", 0))[0]
    ),
)


# ---------------------------------------------------------------------------
# Dependency injection helpers
# ---------------------------------------------------------------------------

def get_client_key(
    request: Request,
    x_api_key: Annotated[str | None, Header()] = None,
) -> str:
    """Extract a stable client identifier from the request."""
    if x_api_key:
        return f"apikey:{x_api_key}"
    client = request.client
    return f"ip:{client.host}" if client else "ip:unknown"


def apply_rate_limit_headers(response: Response, result: RateLimitResult) -> None:
    """Attach standard X-RateLimit-* headers to the response."""
    for key, value in result.headers.items():
        response.headers[key] = value


# ---------------------------------------------------------------------------
# Reusable FastAPI dependency
# ---------------------------------------------------------------------------

class RateLimitDep:
    """FastAPI dependency that enforces a rate limit and injects the result.

    Usage::

        @app.get("/route")
        async def handler(rl: Annotated[RateLimitResult, Depends(RateLimitDep(limiter))]):
            ...
    """

    def __init__(self, limiter: AdaptiveRateLimiter | TokenBucketRateLimiter | SlidingWindowRateLimiter, cost: int = 1):
        self.limiter = limiter
        self.cost = cost

    def __call__(
        self,
        response: Response,
        client_key: Annotated[str, Depends(get_client_key)],
    ) -> RateLimitResult:
        result = self.limiter.is_allowed(client_key, self.cost)
        apply_rate_limit_headers(response, result)

        if not result.allowed:
            raise HTTPException(
                status_code=429,
                detail={
                    "error": "rate_limit_exceeded",
                    "retry_after": result.retry_after,
                    "message": f"Too many requests. Retry after {result.retry_after:.2f}s.",
                },
                headers=result.headers,
            )
        return result


# Instantiate per-endpoint dependencies
api_rate_limit = RateLimitDep(api_limiter)
inference_rate_limit = RateLimitDep(inference_limiter, cost=1)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
async def root():
    return {"message": "smart-ratelimiter FastAPI demo"}


@app.get("/search")
async def search(
    q: str = "",
    rl: Annotated[RateLimitResult, Depends(api_rate_limit)] = ...,  # type: ignore[assignment]
):
    """Search endpoint: 60 req/min per client, adaptive burst up to 180."""
    return {
        "query": q,
        "results": [],
        "rate_limit": {
            "remaining": rl.remaining,
            "reset_after": round(rl.reset_after, 2),
        },
    }


@app.post("/inference")
async def run_inference(
    payload: dict,
    rl: Annotated[RateLimitResult, Depends(inference_rate_limit)] = ...,  # type: ignore[assignment]
):
    """Expensive ML inference: strict 10 req/min, no burst."""
    return {
        "result": "...",
        "tokens_remaining": rl.remaining,
    }


@app.get("/health")
async def health():
    """Health check — not rate limited (middleware skips non-HTTP scopes)."""
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Redis backend example (commented out — requires Redis)
# ---------------------------------------------------------------------------
# To switch to a distributed Redis backend, replace MemoryBackend() with:
#
#   import redis
#   from smart_ratelimiter.backends.redis_backend import RedisBackend
#
#   redis_client = redis.Redis(host="redis", port=6379, decode_responses=True)
#   backend = RedisBackend(client=redis_client, key_prefix="myapp:")
#
#   api_limiter = AdaptiveRateLimiter(backend, limit=60, window=60, burst_multiplier=3)
