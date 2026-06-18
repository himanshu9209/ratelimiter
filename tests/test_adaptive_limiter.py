import concurrent.futures
import time
from unittest.mock import patch
import pytest

# Adjust this import to match your actual library structure
from ratelimiter import AdaptiveRateLimiter, MemoryBackend


class MockSystemMetrics:
    def __init__(self, cpu_percent=10.0, memory_percent=15.0):
        self.cpu_percent = cpu_percent
        self.memory_percent = memory_percent

    def get_metrics(self):
        return {
            "cpu": self.cpu_percent,
            "memory": self.memory_percent
        }


def hammer_limiter(limiter, client_id):
    """Simulates a rapid client request against the limiter."""
    return limiter.is_allowed(client_id)


def run_concurrent_requests(limiter, total_requests, workers=20):
    """Fires a burst of parallel requests to test concurrency safety."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [
            executor.submit(hammer_limiter, limiter, f"user_{i}") 
            for i in range(total_requests)
        ]
        results = [f.result() for f in concurrent.futures.as_completed(futures)]
    return sum(1 for allowed in results if allowed is True)


@patch('psutil.cpu_percent')  # Assuming you use psutil or similar internally
@patch('psutil.virtual_memory')
def test_adaptive_hybrid_dynamic_throttling(mock_virtual_memory, mock_cpu, tmpdir):
    """
    Validates that the Adaptive Hybrid mode tightens limits when system load spikes,
    and returns to normal when system load drops.
    """
    # 1. Setup mock system environment
    metrics = MockSystemMetrics(cpu_percent=10.0, memory_percent=20.0)
    mock_cpu.return_value = metrics.cpu_percent
    # Spoofing psutil's virtual_memory native properties if applicable
    mock_virtual_memory.return_value.percent = metrics.memory_percent

    # Initialize your limiter with a baseline capacity (e.g., 100 requests per 10s)
    # Ensure your library reads the mocked metrics upon initialization or check loops
    backend = MemoryBackend()
    limiter = AdaptiveRateLimiter(backend, limit=100, window=10)

    # ----------------------------------------------------
    # PHASE 1: Baseline Test (Low System Load)
    # ----------------------------------------------------
    # At 10% CPU, the system should easily allow a burst of 50 requests
    allowed_low_load = run_concurrent_requests(limiter, total_requests=50)
    assert allowed_low_load == 50, f"Expected 50 allowed, got {allowed_low_load}"

    # Clear backend state between phases if your library tracks cumulative windows
    backend.clear() 

    # ----------------------------------------------------
    # PHASE 2: Stress Test (High System Load)
    # ----------------------------------------------------
    # Artificially spike the mocked metrics to simulate extreme infrastructure stress
    mock_cpu.return_value = 95.0
    mock_virtual_memory.return_value.percent = 90.0

    # Fire another burst of 50 requests. 
    # Because CPU is at 95%, your Adaptive Hybrid algorithm should tighten limits.
    allowed_high_load = run_concurrent_requests(limiter, total_requests=50)
    
    # Assert that the adaptive mode actively restricted traffic compared to baseline
    assert allowed_high_load < 50, (
        f"Adaptive limiter failed to throttle under stress. Allowed {allowed_high_load}/50 requests."
    )
    print(f"\n[PASSED] Throttled down to {allowed_high_load} requests during 95% CPU spike.")

    backend.clear()

    # ----------------------------------------------------
    # PHASE 3: Recovery Test (Cool Down)
    # ----------------------------------------------------
    # Simulate the server cooling down after traffic subsides
    mock_cpu.return_value = 15.0
    mock_virtual_memory.return_value.percent = 25.0

    # Ensure the library scales back up and allows normal volume again
    allowed_after_recovery = run_concurrent_requests(limiter, total_requests=50)
    assert allowed_after_recovery == 50, (
        f"Limiter failed to recover after system cooldown. Only allowed {allowed_after_recovery}."
    )
