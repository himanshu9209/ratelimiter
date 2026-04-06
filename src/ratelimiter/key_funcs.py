"""
Client identification helpers for rate-limit middleware.

These factory functions return ``key_func`` callables compatible with
both :class:`~ratelimiter.middleware.RateLimitMiddleware` (WSGI) and
:class:`~ratelimiter.middleware.AsyncRateLimitMiddleware` (ASGI).

Using one of these instead of the default IP-based key function lets
you apply **per-user** or **per-API-key** limits rather than a single
global limit, which is critical for multi-tenant services.

WSGI usage (Flask / Django)::

    from ratelimiter.key_funcs import wsgi_api_key_func
    from ratelimiter.middleware import RateLimitMiddleware

    app = RateLimitMiddleware(
        flask_app.wsgi_app,
        limiter=limiter,
        key_func=wsgi_api_key_func("X-API-Key", fallback=wsgi_ip_func()),
    )

ASGI usage (FastAPI / Starlette)::

    from ratelimiter.key_funcs import asgi_api_key_func
    from ratelimiter.middleware import AsyncRateLimitMiddleware

    app.add_middleware(
        AsyncRateLimitMiddleware,
        limiter=limiter,
        key_func=asgi_api_key_func("X-API-Key", fallback=asgi_ip_func()),
    )
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

WsgiKeyFunc = Callable[[Dict[str, Any]], str]
AsgiKeyFunc = Callable[[Dict[str, Any]], str]


# ---------------------------------------------------------------------------
# WSGI key functions
# (receive a PEP 3333 ``environ`` dict, return a string key)
# ---------------------------------------------------------------------------

def wsgi_ip_func(
    trust_x_forwarded_for: bool = True,
) -> WsgiKeyFunc:
    """Return a WSGI key function that extracts the client IP address.

    Args:
        trust_x_forwarded_for: When ``True`` (default), use the first IP
            in the ``X-Forwarded-For`` header when present (for deployments
            behind a reverse proxy or load balancer).  Set to ``False`` to
            always use the direct connection IP (``REMOTE_ADDR``).

    Returns:
        A callable ``(environ) -> str``.

    .. warning::
        ``X-Forwarded-For`` can be spoofed by clients unless your proxy
        strips or overwrites it.  Only set ``trust_x_forwarded_for=True``
        when you control the proxy layer.
    """
    def key_func(environ: Dict[str, Any]) -> str:
        if trust_x_forwarded_for:
            xff = environ.get("HTTP_X_FORWARDED_FOR", "")
            if xff:
                return xff.split(",")[0].strip()
        return environ.get("REMOTE_ADDR", "unknown")

    return key_func


def wsgi_api_key_func(
    header: str = "X-API-Key",
    fallback: Optional[WsgiKeyFunc] = None,
) -> WsgiKeyFunc:
    """Return a WSGI key function that extracts an API key from a request header.

    When the header is absent, delegates to *fallback* (default: remote IP).

    Args:
        header:   The HTTP header name to read (case-insensitive).
                  Typically ``"X-API-Key"`` or ``"Authorization"``.
        fallback: A key function to use when the header is missing.
                  Defaults to :func:`wsgi_ip_func`.

    Returns:
        A callable ``(environ) -> str``.

    Example::

        key_func = wsgi_api_key_func("X-API-Key")
        # Returns the API key value, or the client IP if header is absent.
    """
    # WSGI header name: HTTP_ prefix + uppercased + dashes→underscores
    environ_key = "HTTP_" + header.upper().replace("-", "_")
    _fallback = fallback or wsgi_ip_func()

    def key_func(environ: Dict[str, Any]) -> str:
        api_key = environ.get(environ_key, "").strip()
        if api_key:
            return f"apikey:{api_key}"
        return _fallback(environ)

    return key_func


def wsgi_composite_key_func(*funcs: WsgiKeyFunc, separator: str = "|") -> WsgiKeyFunc:
    """Combine multiple WSGI key functions into a single composite key.

    Useful when you want to rate-limit on a combination of identifiers,
    e.g. ``(api_key, endpoint)`` or ``(ip, user_agent)``.

    Args:
        *funcs:    Two or more key functions to combine.
        separator: String used to join the individual keys. Default ``"|"``.

    Returns:
        A callable ``(environ) -> str`` that returns ``"key1|key2|…"``.
    """
    if len(funcs) < 2:
        raise ValueError("wsgi_composite_key_func requires at least two key functions")

    def key_func(environ: Dict[str, Any]) -> str:
        return separator.join(f(environ) for f in funcs)

    return key_func


# ---------------------------------------------------------------------------
# ASGI key functions
# (receive a Starlette/FastAPI ``scope`` dict, return a string key)
# ---------------------------------------------------------------------------

def _asgi_get_header(scope: Dict[str, Any], header_name: str) -> str:
    """Extract a header value from an ASGI scope's headers list."""
    target = header_name.lower().encode()
    for name, value in scope.get("headers", []):
        if name.lower() == target:
            return value.decode(errors="replace")
    return ""


def asgi_ip_func(
    trust_x_forwarded_for: bool = True,
) -> AsgiKeyFunc:
    """Return an ASGI key function that extracts the client IP address.

    Args:
        trust_x_forwarded_for: When ``True`` (default), honour the
            ``X-Forwarded-For`` header.  Set to ``False`` to always use the
            direct connection peer address from ``scope["client"]``.

    Returns:
        A callable ``(scope) -> str``.
    """
    def key_func(scope: Dict[str, Any]) -> str:
        if trust_x_forwarded_for:
            xff = _asgi_get_header(scope, "x-forwarded-for")
            if xff:
                return xff.split(",")[0].strip()
        client = scope.get("client")
        return client[0] if client else "unknown"

    return key_func


def asgi_api_key_func(
    header: str = "X-API-Key",
    fallback: Optional[AsgiKeyFunc] = None,
) -> AsgiKeyFunc:
    """Return an ASGI key function that extracts an API key from a header.

    When the header is absent, delegates to *fallback* (default: remote IP).

    Args:
        header:   HTTP header name to read (case-insensitive).
        fallback: Key function to use when the header is missing.
                  Defaults to :func:`asgi_ip_func`.

    Returns:
        A callable ``(scope) -> str``.

    Example::

        app.add_middleware(
            AsyncRateLimitMiddleware,
            limiter=limiter,
            key_func=asgi_api_key_func("X-API-Key"),
        )
    """
    _fallback = fallback or asgi_ip_func()

    def key_func(scope: Dict[str, Any]) -> str:
        api_key = _asgi_get_header(scope, header).strip()
        if api_key:
            return f"apikey:{api_key}"
        return _fallback(scope)

    return key_func


def asgi_composite_key_func(*funcs: AsgiKeyFunc, separator: str = "|") -> AsgiKeyFunc:
    """Combine multiple ASGI key functions into a single composite key.

    Args:
        *funcs:    Two or more key functions to combine.
        separator: String used to join individual keys. Default ``"|"``.

    Returns:
        A callable ``(scope) -> str``.
    """
    if len(funcs) < 2:
        raise ValueError("asgi_composite_key_func requires at least two key functions")

    def key_func(scope: Dict[str, Any]) -> str:
        return separator.join(f(scope) for f in funcs)

    return key_func
