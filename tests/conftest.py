"""Shared pytest fixtures."""

import pytest

from smart_ratelimiter.backends.memory import MemoryBackend
from smart_ratelimiter.backends.sqlite_backend import SQLiteBackend


@pytest.fixture
def memory_backend():
    b = MemoryBackend()
    yield b
    b.close()


@pytest.fixture
def sqlite_backend():
    b = SQLiteBackend(db_path=":memory:")
    yield b
    b.close()


@pytest.fixture(params=["memory", "sqlite"])
def backend(request, memory_backend, sqlite_backend):
    """Parametrised fixture — runs each test against every backend."""
    if request.param == "memory":
        return memory_backend
    return sqlite_backend
