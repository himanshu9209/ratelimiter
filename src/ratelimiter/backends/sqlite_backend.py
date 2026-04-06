"""
SQLite storage backend.

Zero extra dependencies — uses the stdlib ``sqlite3`` module.
Suitable for single-host multi-process deployments.  WAL mode is
enabled by default for better concurrent read performance.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast

from ..exceptions import BackendError
from .base import BaseBackend

_CREATE_KV = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_at REAL          -- unix timestamp, NULL = no expiry
);
"""

_CREATE_ZSET = """
CREATE TABLE IF NOT EXISTS zset (
    key    TEXT NOT NULL,
    member TEXT NOT NULL,
    score  REAL NOT NULL,
    PRIMARY KEY (key, member)
);
CREATE INDEX IF NOT EXISTS idx_zset_score ON zset(key, score);
"""


class SQLiteBackend(BaseBackend):
    """SQLite-backed storage for single-host multi-process rate limiting.

    Args:
        db_path: Path to the SQLite database file.
                 Defaults to ``":memory:"`` (in-memory, test-friendly).
        wal_mode: Enable WAL journal mode for better concurrency (default ``True``).

    Example::

        from ratelimiter.backends.sqlite_backend import SQLiteBackend

        backend = SQLiteBackend(db_path="/var/lib/myapp/ratelimiter.db")
    """

    def __init__(
        self,
        db_path: str = ":memory:",
        wal_mode: bool = True,
    ) -> None:
        self._db_path = str(db_path)
        self._local = threading.local()
        self._wal = wal_mode
        # Initialise schema in the calling thread's connection
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection management (one connection per thread)
    # ------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            if self._wal:
                conn.execute("PRAGMA journal_mode = WAL")
            self._local.conn = conn
        return cast(sqlite3.Connection, self._local.conn)

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(_CREATE_KV)
        conn.executescript(_CREATE_ZSET)
        conn.commit()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> float:
        return time.time()

    def _purge_expired(self, conn: sqlite3.Connection, key: str) -> None:
        conn.execute(
            "DELETE FROM kv WHERE key = ? AND expires_at IS NOT NULL AND expires_at <= ?",
            (key, self._now()),
        )

    # ------------------------------------------------------------------
    # BaseBackend implementation
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[Any]:
        try:
            conn = self._conn()
            self._purge_expired(conn, key)
            row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            if row is None:
                return None
            return json.loads(row["value"])
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite GET failed: {exc}") from exc

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        try:
            conn = self._conn()
            expires_at = self._now() + ttl if ttl is not None else None
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(value), expires_at),
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite SET failed: {exc}") from exc

    def delete(self, key: str) -> None:
        try:
            conn = self._conn()
            conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            conn.execute("DELETE FROM zset WHERE key = ?", (key,))
            conn.commit()
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite DELETE failed: {exc}") from exc

    def incr(self, key: str, amount: int = 1) -> int:
        try:
            conn = self._conn()
            self._purge_expired(conn, key)
            row = conn.execute("SELECT value, expires_at FROM kv WHERE key = ?", (key,)).fetchone()
            current = int(json.loads(row["value"])) if row else 0
            new_value = current + amount
            expires_at = row["expires_at"] if row else None
            conn.execute(
                "INSERT OR REPLACE INTO kv (key, value, expires_at) VALUES (?, ?, ?)",
                (key, json.dumps(new_value), expires_at),
            )
            conn.commit()
            return new_value
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite INCR failed: {exc}") from exc

    def expire(self, key: str, ttl: float) -> None:
        try:
            conn = self._conn()
            conn.execute(
                "UPDATE kv SET expires_at = ? WHERE key = ?",
                (self._now() + ttl, key),
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite EXPIRE failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Sorted-set operations
    # ------------------------------------------------------------------

    def zadd(self, key: str, score: float, member: str) -> None:
        try:
            conn = self._conn()
            conn.execute(
                "INSERT OR REPLACE INTO zset (key, member, score) VALUES (?, ?, ?)",
                (key, member, score),
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite ZADD failed: {exc}") from exc

    def zremrangebyscore(self, key: str, min_score: float, max_score: float) -> int:
        try:
            conn = self._conn()
            cursor = conn.execute(
                "DELETE FROM zset WHERE key = ? AND score BETWEEN ? AND ?",
                (key, min_score, max_score),
            )
            conn.commit()
            return cursor.rowcount
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite ZREMRANGEBYSCORE failed: {exc}") from exc

    def zcard(self, key: str) -> int:
        try:
            conn = self._conn()
            row = conn.execute(
                "SELECT COUNT(*) as cnt FROM zset WHERE key = ?", (key,)
            ).fetchone()
            return int(row["cnt"])
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite ZCARD failed: {exc}") from exc

    def zrange_by_score(
        self, key: str, min_score: float, max_score: float
    ) -> list[tuple[str, float]]:
        try:
            conn = self._conn()
            rows = conn.execute(
                "SELECT member, score FROM zset WHERE key = ? AND score BETWEEN ? AND ? ORDER BY score",
                (key, min_score, max_score),
            ).fetchall()
            return [(r["member"], r["score"]) for r in rows]
        except sqlite3.Error as exc:
            raise BackendError(f"SQLite ZRANGE failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
