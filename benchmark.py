"""
Benchmark script for smart-ratelimiter.
Tests throughput (ops/sec) and latency (µs) for every algorithm × backend combination.
"""

import sys
import time
import statistics
import gc

sys.path.insert(0, "src")

from ratelimiter import (
    FixedWindowRateLimiter,
    SlidingWindowRateLimiter,
    SlidingWindowCounterRateLimiter,
    TokenBucketRateLimiter,
    LeakyBucketRateLimiter,
    AdaptiveRateLimiter,
    MemoryBackend,
    SQLiteBackend,
)

WARMUP = 500
ITERATIONS = 5000
LIMIT = 999_999_999  # effectively unlimited so nothing gets rejected
WINDOW = 60.0


def make_limiters(backend_factory):
    return [
        ("FixedWindow",          FixedWindowRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("SlidingWindow",        SlidingWindowRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("SlidingWindowCounter", SlidingWindowCounterRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("TokenBucket",          TokenBucketRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("LeakyBucket",          LeakyBucketRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("Adaptive",             AdaptiveRateLimiter(backend_factory(), LIMIT, WINDOW, burst_multiplier=2)),
    ]


def benchmark(name, limiter, iterations, warmup):
    key = "bench:user"

    # Warmup
    for _ in range(warmup):
        limiter.is_allowed(key)

    gc.disable()
    latencies = []
    start_total = time.perf_counter()

    for _ in range(iterations):
        t0 = time.perf_counter()
        limiter.is_allowed(key)
        t1 = time.perf_counter()
        latencies.append((t1 - t0) * 1_000_000)  # µs

    end_total = time.perf_counter()
    gc.enable()

    elapsed = end_total - start_total
    ops_per_sec = iterations / elapsed
    p50 = statistics.median(latencies)
    p95 = statistics.quantiles(latencies, n=20)[18]  # 95th percentile
    p99 = statistics.quantiles(latencies, n=100)[98]  # 99th percentile
    mean = statistics.mean(latencies)

    return {
        "name": name,
        "ops_per_sec": ops_per_sec,
        "mean_us": mean,
        "p50_us": p50,
        "p95_us": p95,
        "p99_us": p99,
    }


def run_suite(backend_name, backend_factory):
    print(f"\n{'='*60}")
    print(f"  Backend: {backend_name}")
    print(f"{'='*60}")
    print(f"{'Algorithm':<24} {'ops/sec':>10} {'mean µs':>9} {'p50 µs':>9} {'p95 µs':>9} {'p99 µs':>9}")
    print(f"{'-'*24} {'-'*10} {'-'*9} {'-'*9} {'-'*9} {'-'*9}")

    results = []
    for algo_name, limiter in make_limiters(backend_factory):
        r = benchmark(algo_name, limiter, ITERATIONS, WARMUP)
        results.append(r)
        print(
            f"{r['name']:<24} "
            f"{r['ops_per_sec']:>10,.0f} "
            f"{r['mean_us']:>9.2f} "
            f"{r['p50_us']:>9.2f} "
            f"{r['p95_us']:>9.2f} "
            f"{r['p99_us']:>9.2f}"
        )
    return results


if __name__ == "__main__":
    print("smart-ratelimiter benchmark")
    print(f"Warmup: {WARMUP} | Iterations: {ITERATIONS} per algorithm")

    memory_results = run_suite("In-Memory", MemoryBackend)
    sqlite_results = run_suite("SQLite (WAL)", lambda: SQLiteBackend(db_path=":memory:"))

    print(f"\n{'='*60}")
    print("Done.")
