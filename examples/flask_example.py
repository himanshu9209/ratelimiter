"""
Flask example — smart-ratelimiter
==================================

Demonstrates three patterns:

1. WSGI middleware (global rate limit applied to every request)
2. @rate_limit decorator (per-endpoint limit)
3. Manual is_allowed() check with custom JSON error response

Run:
    pip install flask smart-ratelimiter
    python examples/flask_example.py
"""

from __future__ import annotations

from flask import Flask, g, jsonify, request

from ratelimiter import (
    AdaptiveRateLimiter,
    MemoryBackend,
    RateLimitExceeded,
    SlidingWindowRateLimiter,
    rate_limit,
)
from ratelimiter.middleware import RateLimitMiddleware

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Limiters
# ---------------------------------------------------------------------------

# Global limiter: 200 req/min per IP — applied via WSGI middleware
global_limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=200, window=60)

# Endpoint-specific limiter: 5 req/min for the search endpoint
search_limiter = AdaptiveRateLimiter(
    MemoryBackend(),
    limit=5,
    window=60,
    burst_multiplier=2,   # allow up to 10 burst when traffic is quiet
)

# Heavy endpoint limiter: strict token bucket, 1 req/s sustained
export_limiter = AdaptiveRateLimiter(MemoryBackend(), limit=1, window=1)


# ---------------------------------------------------------------------------
# Helper: extract rate-limit key from request
# ---------------------------------------------------------------------------

def client_key() -> str:
    """Use X-API-Key header if present, otherwise fall back to remote IP."""
    return request.headers.get("X-API-Key") or request.remote_addr or "unknown"


# ---------------------------------------------------------------------------
# Pattern 1: WSGI middleware (wraps the whole app)
# ---------------------------------------------------------------------------

app.wsgi_app = RateLimitMiddleware(  # type: ignore[assignment]
    app.wsgi_app,
    limiter=global_limiter,
    key_func=lambda env: (
        env.get("HTTP_X_API_KEY")
        or env.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or env.get("REMOTE_ADDR", "unknown")
    ),
)


# ---------------------------------------------------------------------------
# Pattern 2: @rate_limit decorator
# ---------------------------------------------------------------------------

@app.route("/search")
@rate_limit(search_limiter, key_func=lambda: client_key())
def search():
    """Rate-limited search endpoint: 5 req/min (burst up to 10 when quiet)."""
    query = request.args.get("q", "")
    return jsonify({"results": [], "query": query})


# ---------------------------------------------------------------------------
# Pattern 3: Manual is_allowed() check
# ---------------------------------------------------------------------------

@app.route("/export/<report_id>")
def export(report_id: str):
    """Heavy endpoint: 1 req/s per API key. Returns 429 with Retry-After."""
    key = f"export:{client_key()}"
    result = export_limiter.is_allowed(key)

    if not result.allowed:
        response = jsonify({
            "error": "rate_limit_exceeded",
            "message": f"Export is limited to 1 request/s. "
                       f"Retry after {result.retry_after:.2f}s.",
            "retry_after": result.retry_after,
        })
        response.status_code = 429
        response.headers.update(result.headers)
        return response

    # Simulate expensive report generation
    response = jsonify({"report_id": report_id, "status": "generating"})
    response.headers.update(result.headers)
    return response


# ---------------------------------------------------------------------------
# Error handler for @rate_limit decorator raises
# ---------------------------------------------------------------------------

@app.errorhandler(RateLimitExceeded)
def handle_rate_limit(exc: RateLimitExceeded):
    response = jsonify({
        "error": "rate_limit_exceeded",
        "message": str(exc),
        "retry_after": exc.retry_after,
    })
    response.status_code = 429
    response.headers["Retry-After"] = f"{exc.retry_after:.2f}"
    return response


# ---------------------------------------------------------------------------
# Health check (not rate-limited)
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


if __name__ == "__main__":
    print("Flask rate-limiter demo running on http://127.0.0.1:5000")
    print("  GET /search?q=test     — 5 req/min per client (burst 10)")
    print("  GET /export/<id>       — 1 req/s per client")
    print("  GET /health            — unlimited")
    app.run(debug=True)
