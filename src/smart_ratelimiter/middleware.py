"""
WSGI and ASGI middleware for HTTP rate limiting.

WSGI example (Flask / Django)::

    from smart_ratelimiter.middleware import RateLimitMiddleware
    app = RateLimitMiddleware(app, limiter=limiter, key_func=lambda env: env.get("REMOTE_ADDR"))

ASGI example (FastAPI / Starlette)::

    from smart_ratelimiter.middleware import AsyncRateLimitMiddleware
    app.add_middleware(AsyncRateLimitMiddleware, limiter=limiter)
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional, cast

from .algorithms.base import BaseAlgorithm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_key_func(environ: dict[str, Any]) -> str:
    """Extract a rate-limit key from a WSGI environ dict."""
    # X-Forwarded-For if behind a proxy, else REMOTE_ADDR
    xff = str(environ.get("HTTP_X_FORWARDED_FOR", ""))
    if xff:
        return xff.split(",")[0].strip()
    return str(environ.get("REMOTE_ADDR", "unknown"))


def _rate_limit_response(
    retry_after: float,
    start_response: Callable[..., Any],
) -> list[bytes]:
    headers = [
        ("Content-Type", "application/json"),
        ("Retry-After", f"{retry_after:.2f}"),
    ]
    start_response("429 Too Many Requests", headers)
    return [b'{"error": "rate limit exceeded"}']


# ---------------------------------------------------------------------------
# WSGI middleware
# ---------------------------------------------------------------------------

class RateLimitMiddleware:
    """PEP 3333–compliant WSGI middleware.

    Args:
        app:          The inner WSGI application.
        limiter:      Configured rate-limiter instance.
        key_func:     Callable ``(environ) -> str`` to extract the rate-limit
                      key from the request environment.
        cost:         Request cost per call (default 1).
        status_code:  HTTP status returned on rejection (default 429).

    Example::

        from flask import Flask
        from smart_ratelimiter.backends.memory import MemoryBackend
        from smart_ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
        from smart_ratelimiter.middleware import RateLimitMiddleware

        flask_app = Flask(__name__)
        limiter = SlidingWindowRateLimiter(MemoryBackend(), limit=60, window=60)
        wsgi_app = RateLimitMiddleware(flask_app.wsgi_app, limiter=limiter)
    """

    def __init__(
        self,
        app: Callable[..., Any],
        limiter: BaseAlgorithm,
        key_func: Optional[Callable[[dict[str, Any]], str]] = None,
        cost: int = 1,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.key_func = key_func or _default_key_func
        self.cost = cost

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Callable[..., Any],
    ) -> Iterable[bytes]:
        key = self.key_func(environ)
        result = self.limiter.is_allowed(key, self.cost)

        if not result.allowed:
            return _rate_limit_response(result.retry_after, start_response)

        # Inject rate-limit headers into the response
        def patched_start_response(
            status: str, headers: list[tuple[str, str]], *args: Any
        ) -> Any:
            headers += list(result.headers.items())
            return start_response(status, headers, *args)

        return cast(Iterable[bytes], self.app(environ, patched_start_response))


# ---------------------------------------------------------------------------
# ASGI middleware
# ---------------------------------------------------------------------------

class AsyncRateLimitMiddleware:
    """ASGI middleware compatible with Starlette / FastAPI.

    Args:
        app:      Inner ASGI application.
        limiter:  Configured rate-limiter instance.
        key_func: Async or sync callable ``(scope) -> str`` to extract key.
        cost:     Request cost per call (default 1).

    Example::

        from fastapi import FastAPI
        from smart_ratelimiter.backends.redis_backend import RedisBackend
        from smart_ratelimiter.algorithms.adaptive import AdaptiveRateLimiter
        from smart_ratelimiter.middleware import AsyncRateLimitMiddleware

        fast_app = FastAPI()
        limiter = AdaptiveRateLimiter(RedisBackend(), limit=100, window=60)
        fast_app.add_middleware(AsyncRateLimitMiddleware, limiter=limiter)
    """

    def __init__(
        self,
        app: Any,
        limiter: BaseAlgorithm,
        key_func: Optional[Callable[..., Any]] = None,
        cost: int = 1,
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.key_func = key_func
        self.cost = cost

    async def __call__(
        self, scope: dict[str, Any], receive: Any, send: Any
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Extract key
        if self.key_func:
            import inspect
            if inspect.iscoroutinefunction(self.key_func):
                key = await self.key_func(scope)
            else:
                key = self.key_func(scope)
        else:
            # Default: client IP from scope
            client = scope.get("client")
            key = client[0] if client else "unknown"

        result = self.limiter.is_allowed(key, self.cost)

        if not result.allowed:
            body = b'{"error": "rate limit exceeded"}'
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"retry-after", f"{result.retry_after:.2f}".encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        # Forward to inner app, injecting headers
        rl_headers = [
            (k.lower().encode(), v.encode()) for k, v in result.headers.items()
        ]

        async def patched_send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + rl_headers
            await send(message)

        await self.app(scope, receive, patched_send)
