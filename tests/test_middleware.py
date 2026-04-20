"""
Tests for RateLimitMiddleware (WSGI) and AsyncRateLimitMiddleware (ASGI).

No framework dependencies — tests drive the middleware directly using raw
WSGI callables and minimal ASGI scope/receive/send objects.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from smart_ratelimiter.algorithms.fixed_window import FixedWindowRateLimiter
from smart_ratelimiter.backends.memory import MemoryBackend
from smart_ratelimiter.middleware import AsyncRateLimitMiddleware, RateLimitMiddleware


# ---------------------------------------------------------------------------
# WSGI helpers
# ---------------------------------------------------------------------------

def _make_environ(remote_addr: str = "1.2.3.4", xff: str = "") -> dict[str, Any]:
    env: dict[str, Any] = {"REMOTE_ADDR": remote_addr}
    if xff:
        env["HTTP_X_FORWARDED_FOR"] = xff
    return env


def _run_wsgi(app: Any, environ: dict[str, Any]) -> tuple[str, list[tuple[str, str]], bytes]:
    """Drive a WSGI app and return (status, headers, body)."""
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]], *args: Any) -> None:
        captured["status"] = status
        captured["headers"] = headers

    body = b"".join(app(environ, start_response))
    return captured["status"], captured["headers"], body


def _simple_wsgi_app(environ: Any, start_response: Any) -> list[bytes]:
    start_response("200 OK", [("Content-Type", "text/plain")])
    return [b"ok"]


# ---------------------------------------------------------------------------
# WSGI tests
# ---------------------------------------------------------------------------

class TestRateLimitMiddleware:
    def _make(self, limit: int = 5, key_func: Any = None) -> RateLimitMiddleware:
        limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=60.0)
        return RateLimitMiddleware(_simple_wsgi_app, limiter=limiter, key_func=key_func)

    def test_allowed_request_passes_through(self):
        mw = self._make(limit=5)
        status, _, body = _run_wsgi(mw, _make_environ())
        assert status.startswith("200")
        assert body == b"ok"

    def test_rejected_request_returns_429(self):
        mw = self._make(limit=2)
        env = _make_environ()
        _run_wsgi(mw, env)
        _run_wsgi(mw, env)
        status, headers, body = _run_wsgi(mw, env)
        assert status.startswith("429")
        header_names = [h[0] for h in headers]
        assert "Retry-After" in header_names
        assert b"rate limit exceeded" in body.lower()

    def test_rate_limit_headers_injected_on_success(self):
        mw = self._make(limit=10)
        _, headers, _ = _run_wsgi(mw, _make_environ())
        header_names = [h[0] for h in headers]
        assert "X-RateLimit-Limit" in header_names
        assert "X-RateLimit-Remaining" in header_names
        assert "X-RateLimit-Reset" in header_names

    def test_default_key_func_uses_remote_addr(self):
        mw = self._make(limit=2)
        _run_wsgi(mw, _make_environ(remote_addr="10.0.0.1"))
        _run_wsgi(mw, _make_environ(remote_addr="10.0.0.1"))
        # Third from same IP → 429
        status, _, _ = _run_wsgi(mw, _make_environ(remote_addr="10.0.0.1"))
        assert status.startswith("429")
        # Different IP is unaffected
        status2, _, _ = _run_wsgi(mw, _make_environ(remote_addr="10.0.0.2"))
        assert status2.startswith("200")

    def test_default_key_func_prefers_xff(self):
        mw = self._make(limit=2)
        env = _make_environ(remote_addr="proxy", xff="real-client")
        _run_wsgi(mw, env)
        _run_wsgi(mw, env)
        status, _, _ = _run_wsgi(mw, env)
        assert status.startswith("429")
        # Different XFF → separate bucket
        env2 = _make_environ(remote_addr="proxy", xff="other-client")
        status2, _, _ = _run_wsgi(mw, env2)
        assert status2.startswith("200")

    def test_custom_key_func(self):
        mw = self._make(limit=1, key_func=lambda env: env.get("HTTP_X_TENANT", "default"))
        env_a = {**_make_environ(), "HTTP_X_TENANT": "tenant-a"}
        env_b = {**_make_environ(), "HTTP_X_TENANT": "tenant-b"}
        _run_wsgi(mw, env_a)
        # tenant-a exhausted
        assert _run_wsgi(mw, env_a)[0].startswith("429")
        # tenant-b independent
        assert _run_wsgi(mw, env_b)[0].startswith("200")

    def test_cost_parameter(self):
        limiter = FixedWindowRateLimiter(MemoryBackend(), limit=5, window=60.0)
        mw = RateLimitMiddleware(_simple_wsgi_app, limiter=limiter, cost=3)
        _run_wsgi(mw, _make_environ())  # consumes 3
        status, _, _ = _run_wsgi(mw, _make_environ())  # needs 3, only 2 left → 429
        assert status.startswith("429")

    def test_retry_after_header_value_is_numeric(self):
        mw = self._make(limit=1)
        env = _make_environ()
        _run_wsgi(mw, env)
        _, headers, _ = _run_wsgi(mw, env)
        retry_after = dict(headers).get("Retry-After", "")
        assert float(retry_after) > 0


# ---------------------------------------------------------------------------
# ASGI helpers
# ---------------------------------------------------------------------------

def _make_http_scope(client_ip: str = "1.2.3.4") -> dict[str, Any]:
    return {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [],
        "client": (client_ip, 12345),
    }


async def _simple_asgi_app(scope: Any, receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _run_asgi(
    app: Any, scope: dict[str, Any]
) -> tuple[int, dict[bytes, bytes], bytes]:
    """Drive an ASGI app and return (status, headers_dict, body)."""
    messages: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app(scope, receive, send)

    start = next(m for m in messages if m["type"] == "http.response.start")
    body_msg = next(m for m in messages if m["type"] == "http.response.body")
    headers = {k: v for k, v in start.get("headers", [])}
    return start["status"], headers, body_msg.get("body", b"")


# ---------------------------------------------------------------------------
# ASGI tests
# ---------------------------------------------------------------------------

class TestAsyncRateLimitMiddleware:
    def _make(self, limit: int = 5, key_func: Any = None) -> AsyncRateLimitMiddleware:
        limiter = FixedWindowRateLimiter(MemoryBackend(), limit=limit, window=60.0)
        return AsyncRateLimitMiddleware(_simple_asgi_app, limiter=limiter, key_func=key_func)

    @pytest.mark.asyncio
    async def test_allowed_request_passes_through(self):
        mw = self._make(limit=5)
        status, _, body = await _run_asgi(mw, _make_http_scope())
        assert status == 200
        assert body == b"ok"

    @pytest.mark.asyncio
    async def test_rejected_request_returns_429(self):
        mw = self._make(limit=2)
        scope = _make_http_scope()
        await _run_asgi(mw, scope)
        await _run_asgi(mw, scope)
        status, headers, body = await _run_asgi(mw, scope)
        assert status == 429
        assert b"rate limit exceeded" in body.lower()
        assert b"retry-after" in headers

    @pytest.mark.asyncio
    async def test_rate_limit_headers_on_success(self):
        mw = self._make(limit=10)
        _, headers, _ = await _run_asgi(mw, _make_http_scope())
        assert b"x-ratelimit-limit" in headers
        assert b"x-ratelimit-remaining" in headers

    @pytest.mark.asyncio
    async def test_non_http_scope_passes_through(self):
        """WebSocket and lifespan scopes bypass rate limiting entirely."""
        reached = False

        async def inner(scope: Any, receive: Any, send: Any) -> None:
            nonlocal reached
            reached = True

        limiter = FixedWindowRateLimiter(MemoryBackend(), limit=1, window=60.0)
        # Exhaust the limiter so any HTTP request would be rejected
        limiter.is_allowed("x")
        limiter.is_allowed("x")

        mw = AsyncRateLimitMiddleware(inner, limiter=limiter)
        ws_scope = {"type": "websocket", "path": "/ws", "headers": [], "client": ("1.2.3.4", 0)}
        await mw(ws_scope, None, None)
        assert reached

    @pytest.mark.asyncio
    async def test_default_key_extracts_client_ip(self):
        mw = self._make(limit=1)
        await _run_asgi(mw, _make_http_scope(client_ip="5.5.5.5"))
        status, _, _ = await _run_asgi(mw, _make_http_scope(client_ip="5.5.5.5"))
        assert status == 429
        # Different IP is unaffected
        status2, _, _ = await _run_asgi(mw, _make_http_scope(client_ip="6.6.6.6"))
        assert status2 == 200

    @pytest.mark.asyncio
    async def test_custom_sync_key_func(self):
        key_func = lambda scope: scope["client"][0]  # noqa: E731
        mw = self._make(limit=1, key_func=key_func)
        await _run_asgi(mw, _make_http_scope(client_ip="a.a.a.a"))
        status, _, _ = await _run_asgi(mw, _make_http_scope(client_ip="a.a.a.a"))
        assert status == 429

    @pytest.mark.asyncio
    async def test_custom_async_key_func(self):
        async def key_func(scope: Any) -> str:
            return scope["client"][0]

        mw = self._make(limit=1, key_func=key_func)
        await _run_asgi(mw, _make_http_scope(client_ip="b.b.b.b"))
        status, _, _ = await _run_asgi(mw, _make_http_scope(client_ip="b.b.b.b"))
        assert status == 429

    @pytest.mark.asyncio
    async def test_retry_after_header_numeric(self):
        mw = self._make(limit=1)
        scope = _make_http_scope()
        await _run_asgi(mw, scope)
        _, headers, _ = await _run_asgi(mw, scope)
        retry_val = headers.get(b"retry-after", b"0")
        assert float(retry_val) > 0
