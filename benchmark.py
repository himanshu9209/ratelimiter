"""
Benchmark script for smart-ratelimiter.

Two suites:
  1. Single-threaded — throughput (ops/sec) and latency percentiles.
  2. Multi-threaded  — N threads each hammering is_allowed() concurrently.
                       Shows the real-world cost of lock contention.
"""

import sys
import time
import statistics
import gc
import threading
import concurrent.futures

sys.path.insert(0, "src")

from smart_ratelimiter import (
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

# Thread counts to test in the concurrent suite
THREAD_COUNTS = [1, 2, 4, 8]
CONCURRENT_ITERATIONS = 2000  # per thread


def make_limiters(backend_factory):
    return [
        ("FixedWindow",          FixedWindowRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("SlidingWindow",        SlidingWindowRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("SlidingWindowCounter", SlidingWindowCounterRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("TokenBucket",          TokenBucketRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("LeakyBucket",          LeakyBucketRateLimiter(backend_factory(), LIMIT, WINDOW)),
        ("Adaptive",             AdaptiveRateLimiter(backend_factory(), LIMIT, WINDOW, burst_multiplier=2)),
    ]


# ---------------------------------------------------------------------------
# Single-threaded benchmark
# ---------------------------------------------------------------------------

def benchmark(name, limiter, iterations, warmup):
    key = "bench:user"

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
    p95 = statistics.quantiles(latencies, n=20)[18]
    p99 = statistics.quantiles(latencies, n=100)[98]
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


# ---------------------------------------------------------------------------
# Multi-threaded benchmark
# ---------------------------------------------------------------------------

def _thread_worker(limiter, key: str, iterations: int, barrier: threading.Barrier):
    """Worker: wait for all threads to be ready, then hammer is_allowed()."""
    barrier.wait()  # synchronise start so all threads hit simultaneously
    start = time.perf_counter()
    for _ in range(iterations):
        limiter.is_allowed(key)
    return time.perf_counter() - start


def benchmark_concurrent(limiter, n_threads: int, iterations_per_thread: int):
    """Return aggregate ops/sec across all threads."""
    barrier = threading.Barrier(n_threads)
    total_iterations = n_threads * iterations_per_thread

    with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as pool:
        futures = [
            pool.submit(
                _thread_worker,
                limiter,
                f"bench:user:{t % 4}",  # 4 distinct keys to show key-shard behaviour
                iterations_per_thread,
                barrier,
            )
            for t in range(n_threads)
        ]
        wall_times = [f.result() for f in futures]

    # Total elapsed ≈ max individual time (all started in sync)
    elapsed = max(wall_times)
    return total_iterations / elapsed


def run_concurrent_suite():
    print(f"\n{'='*70}")
    print("  Concurrent benchmark — MemoryBackend only")
    print(f"  {CONCURRENT_ITERATIONS} iterations/thread, keys spread across 4 buckets")
    print(f"{'='*70}")

    header = f"{'Algorithm':<24}"
    for t in THREAD_COUNTS:
        header += f" {f'{t}T ops/s':>12}"
    print(header)
    print(f"{'-'*24}" + f" {'-'*12}" * len(THREAD_COUNTS))

    algo_names_limiters = make_limiters(MemoryBackend)

    for algo_name, limiter in algo_names_limiters:
        row = f"{algo_name:<24}"
        for n_threads in THREAD_COUNTS:
            ops = benchmark_concurrent(limiter, n_threads, CONCURRENT_ITERATIONS)
            row += f" {ops:>12,.0f}"
        print(row)

    print()
    print("  Interpretation:")
    print("  * 1T = baseline single-threaded throughput.")
    print("  * If 4T ~= 4x1T -> near-linear scaling (shard locks help).")
    print("  * If 4T ~= 1T   -> single hot lock is the bottleneck.")
    print("  * GIL means pure-Python ops may not scale past 2-3 threads regardless.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("smart-ratelimiter benchmark")
    print(f"Warmup: {WARMUP} | Iterations: {ITERATIONS} per algorithm")

    run_suite("In-Memory", MemoryBackend)
    run_suite("SQLite (WAL)", lambda: SQLiteBackend(db_path=":memory:"))
    run_concurrent_suite()

    print(f"\n{'='*60}")
    print("Done.")
