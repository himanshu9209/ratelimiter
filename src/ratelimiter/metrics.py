"""
Observability support for rate limiters.

Provides a :class:`MetricsCollector` protocol, an in-process
:class:`InMemoryMetricsCollector` implementation, and an
:class:`ObservableRateLimiter` wrapper that records every rate-limit
decision without requiring any changes to the underlying algorithm.

Why metrics matter
------------------
Tracking *dropped* requests (HTTP 429s) is essential for SRE work:

* A rising drop rate during normal hours signals a limit that is too
  tight — users are being unfairly throttled.
* A sudden spike in dropped requests from a handful of keys suggests a
  DoS or credential-stuffing attempt.
* A near-zero drop rate long-term may indicate the limiter is
  misconfigured and not enforcing anything meaningful.

Usage::

    from ratelimiter.backends.memory import MemoryBackend
    from ratelimiter.algorithms.sliding_window import SlidingWindowRateLimiter
    from ratelimiter.metrics import InMemoryMetricsCollector, ObservableRateLimiter

    metrics = InMemoryMetricsCollector()
    limiter = ObservableRateLimiter(
        SlidingWindowRateLimiter(MemoryBackend(), limit=10, window=1),
        metrics,
    )

    for _ in range(15):
        limiter.is_allowed("user:1")

    stats = metrics.get_stats()
    print(stats["allowed"])   # 10
    print(stats["dropped"])   # 5
    print(stats["drop_rate"]) # 0.333...

Extend for production
---------------------
Subclass :class:`MetricsCollector` to push metrics to Prometheus,
StatsD, DataDog, or any other backend::

    class PrometheusCollector(MetricsCollector):
        def record(self, key, result):
            if result.allowed:
                ALLOWED_COUNTER.labels(key=key).inc()
            else:
                DROPPED_COUNTER.labels(key=key).inc()
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, Dict, Optional

from .algorithms.base import RateLimitResult


class MetricsCollector(ABC):
    """Abstract base for rate-limiter metrics collectors.

    Implement :meth:`record` to forward metrics to any backend
    (Prometheus, StatsD, CloudWatch, …).
    """

    @abstractmethod
    def record(self, key: str, result: RateLimitResult) -> None:
        """Record the outcome of a single rate-limit check.

        Args:
            key:    The rate-limit key that was checked (e.g. a user ID or IP).
            result: The full :class:`~ratelimiter.algorithms.base.RateLimitResult`.
        """


class InMemoryMetricsCollector(MetricsCollector):
    """Thread-safe, in-process metrics store.

    Keeps per-key and global counters for allowed and dropped requests.
    Suitable for dashboards, health endpoints, and alerting within a
    single process.  For distributed or long-lived deployments, replace
    with a :class:`MetricsCollector` that writes to an external system.

    Example::

        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(base_limiter, metrics)

        limiter.is_allowed("192.168.1.1")
        stats = metrics.get_stats()          # global totals
        per_ip = metrics.get_stats("192.168.1.1")  # per-key breakdown
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._allowed: Dict[str, int] = defaultdict(int)
        self._dropped: Dict[str, int] = defaultdict(int)

    # ------------------------------------------------------------------
    # MetricsCollector
    # ------------------------------------------------------------------

    def record(self, key: str, result: RateLimitResult) -> None:
        with self._lock:
            if result.allowed:
                self._allowed[key] += 1
            else:
                self._dropped[key] += 1

    # ------------------------------------------------------------------
    # Querying
    # ------------------------------------------------------------------

    def get_stats(self, key: Optional[str] = None) -> Dict[str, Any]:
        """Return a metrics snapshot.

        Args:
            key: When supplied, return stats for that key only.
                 When ``None`` (default), return global totals plus a
                 ``per_key`` breakdown of every observed key.

        Returns:
            A dictionary with at least the following keys:

            * ``allowed``   — number of requests that were permitted.
            * ``dropped``   — number of requests that were rejected (429).
            * ``total``     — ``allowed + dropped``.
            * ``drop_rate`` — fraction of requests that were dropped (0–1).
        """
        with self._lock:
            if key is not None:
                allowed = self._allowed.get(key, 0)
                dropped = self._dropped.get(key, 0)
                total = allowed + dropped
                return {
                    "key": key,
                    "allowed": allowed,
                    "dropped": dropped,
                    "total": total,
                    "drop_rate": dropped / total if total > 0 else 0.0,
                }

            total_allowed = sum(self._allowed.values())
            total_dropped = sum(self._dropped.values())
            total = total_allowed + total_dropped
            return {
                "allowed": total_allowed,
                "dropped": total_dropped,
                "total": total,
                "drop_rate": total_dropped / total if total > 0 else 0.0,
                "per_key": {
                    k: {
                        "allowed": self._allowed.get(k, 0),
                        "dropped": self._dropped.get(k, 0),
                    }
                    for k in set(self._allowed) | set(self._dropped)
                },
            }

    def reset(self, key: Optional[str] = None) -> None:
        """Clear recorded metrics.

        Args:
            key: Clear only the counters for this key.
                 If ``None``, clear all counters.
        """
        with self._lock:
            if key is not None:
                self._allowed.pop(key, None)
                self._dropped.pop(key, None)
            else:
                self._allowed.clear()
                self._dropped.clear()


class ObservableRateLimiter:
    """Non-intrusive metrics wrapper for any rate limiter.

    Delegates every call to the inner limiter, then records the outcome
    in the supplied :class:`MetricsCollector`.  The inner limiter is
    completely unmodified; no algorithm changes are required.

    Args:
        limiter:  Any object with an ``is_allowed(key, cost)`` method
                  (typically a :class:`~ratelimiter.algorithms.base.BaseAlgorithm`).
        metrics:  The collector that will receive each decision.

    Example::

        metrics = InMemoryMetricsCollector()
        limiter = ObservableRateLimiter(
            FixedWindowRateLimiter(MemoryBackend(), limit=100, window=60),
            metrics,
        )

        result = limiter.is_allowed("user:42")
        # metrics.get_stats("user:42") now shows 1 allowed or dropped
    """

    def __init__(self, limiter: Any, metrics: MetricsCollector) -> None:
        self._limiter = limiter
        self._metrics = metrics

    def is_allowed(self, key: str, cost: int = 1) -> RateLimitResult:
        result = self._limiter.is_allowed(key, cost)
        self._metrics.record(key, result)
        return result

    def reset(self, key: str) -> None:
        """Delegate reset to the inner limiter."""
        self._limiter.reset(key)

    def __getattr__(self, name: str) -> Any:
        # Proxy attribute access so callers can still read .limit, .window, etc.
        return getattr(self._limiter, name)
