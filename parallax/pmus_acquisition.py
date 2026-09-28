"""Shared, fail-closed Polymarket US acquisition policy for sports scans.

The unattended scheduler launches each sports lane in a fresh subprocess.  A
small SQLite state file therefore owns cross-cycle reference/book caches, the
global request budget, and rate-limit circuit state.  Cached executable data
always retains its original observation time; this module never makes a stale
book look fresh.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import threading
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from maker_spread_economics.polymarket_us import PolymarketUSRateLimit

from .engine import MAX_AGE_SECONDS, MAX_SPREAD, MIN_EDGE


DEFAULT_STATE_PATH = Path("data/parallax-commercial/pmus_acquisition.sqlite")
# One 60-minute sports interval plus five minutes of scheduler jitter.  A
# second unattended cycle may reuse stable identity/reference rows, but the
# following cycle must rediscover them.
DISCOVERY_TTL_SECONDS = 65 * 60
# The unattended owner wakes every five minutes; failures are reconsidered on
# that existing operational cadence rather than retry-looped in one scan.
NEGATIVE_TTL_SECONDS = 5 * 60
CIRCUIT_LOCKOUT_SECONDS = 5 * 60
# Only a quote at least one full existing MAX_SPREAD beyond the applicable edge
# gate can use this longer reference lifetime.
OBVIOUS_NONCANDIDATE_TTL_SECONDS = 65 * 60
# GetOrderBook/GetBBO is documented at 12/minute.  Eight books/minute leaves a
# one-third safety margin; the total-public-call bucket is still capped at ten.
TOTAL_REQUESTS_PER_MINUTE = 10
BOOK_REQUESTS_PER_MINUTE = 8
BOOK_REQUESTS_PER_LANE = 4
RESERVED_CONFIRMATION_BOOKS = 2
RESERVED_CONFIRMATION_BOOKS_PER_LANE = 1
TOKEN_CAPACITY = 2.0
TOKEN_REFILL_PER_SECOND = TOTAL_REQUESTS_PER_MINUTE / 60.0
REQUEST_DEADLINE_SECONDS = 7.0
LOCK_POLL_SECONDS = 0.05
PRIORITY_CANDIDATE = 50
PRIORITY_DISCOVERY = 80
PRIORITY_CONFIRMATION = 100


class PMUSAcquisitionUnavailable(RuntimeError):
    """No safe provider request or cached result is currently available."""


class PMUSCircuitOpen(PolymarketUSRateLimit):
    """The persisted PMUS rate-limit circuit is open."""


@dataclass(frozen=True)
class BookAcquisition:
    book: dict[str, Any] | None
    observed_at: str | None
    source: str
    requested: bool = False
    avoided_reason: str | None = None


def _utc_iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _is_rate_limit(exc: BaseException) -> bool:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = " ".join(
            (
                str(current),
                str(getattr(current, "body", "")),
                str(getattr(current, "status_code", "")),
            )
        ).casefold()
        if (
            isinstance(current, PolymarketUSRateLimit)
            or getattr(current, "status_code", None) == 429
            or "1015" in text
            or "rate limit" in text
            or "rate-limit" in text
            or "too many requests" in text
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def _retry_after(exc: BaseException) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if headers else None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _book_ask(book: dict[str, Any], slug: str, side: str) -> float | None:
    row = book.get(f"{slug}::{side}")
    if not isinstance(row, dict):
        return None
    try:
        value = float(row.get("best_ask"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and 0 < value < 1 else None


class PMUSAcquisition:
    """Cross-process cache, token bucket, deadline policy, and circuit breaker."""

    def __init__(
        self,
        lane: str,
        *,
        state_path: str | Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        configured = os.environ.get("PARALLAX_PMUS_ACQUISITION_DB")
        self.path = Path(state_path or configured or DEFAULT_STATE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_dir = self.path.parent / f".{self.path.name}.locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.lane = lane.upper()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleeper = sleeper
        self.metrics: Counter[str] = Counter()
        self._book_requests = 0
        self._locks_guard = threading.Lock()
        self._key_locks: dict[str, threading.Lock] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS cache (
                    cache_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    payload TEXT,
                    observed_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    error_type TEXT
                );
                CREATE TABLE IF NOT EXISTS request_log (
                    requested_at REAL NOT NULL,
                    kind TEXT NOT NULL,
                    lane TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 50
                );
                CREATE INDEX IF NOT EXISTS request_log_time
                    ON request_log(requested_at);
                CREATE TABLE IF NOT EXISTS governor (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    tokens REAL NOT NULL,
                    last_refill REAL NOT NULL,
                    circuit_until REAL NOT NULL,
                    circuit_reason TEXT
                );
                """
            )
            now = self._epoch()
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(request_log)")
            }
            if "priority" not in columns:
                connection.execute(
                    "ALTER TABLE request_log ADD COLUMN priority INTEGER NOT NULL DEFAULT 50"
                )
            connection.execute(
                "INSERT OR IGNORE INTO governor VALUES (1, ?, ?, 0, NULL)",
                (TOKEN_CAPACITY, now),
            )

    def _epoch(self) -> float:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("PMUS acquisition clock must be timezone-aware")
        return value.timestamp()

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._key_locks.setdefault(key, threading.Lock())

    def _wait_for_key_lock(self, deadline: float, key: str) -> None:
        now = self._epoch()
        if now >= deadline:
            self.metrics["requests_avoided"] += 1
            raise PMUSAcquisitionUnavailable(
                f"PMUS cache-key lock deferred beyond decision deadline: {key}"
            )
        self.sleeper(min(LOCK_POLL_SECONDS, deadline - now))

    @contextmanager
    def _singleflight(self, key: str):
        """Serialize one cache key across threads and local-host processes."""
        deadline = self._epoch() + REQUEST_DEADLINE_SECONDS
        thread_lock = self._lock_for(key)
        while not thread_lock.acquire(blocking=False):
            self._wait_for_key_lock(deadline, key)
        try:
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
            lock_path = self.lock_dir / f"{digest}.lock"
            with lock_path.open("a+b") as lock_file:
                while True:
                    try:
                        fcntl.flock(
                            lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                        )
                        break
                    except BlockingIOError:
                        self._wait_for_key_lock(deadline, key)
                try:
                    yield
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            thread_lock.release()

    def _cache_get(self, key: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM cache WHERE cache_key = ?", (key,)
            ).fetchone()

    def _cache_put(
        self,
        key: str,
        *,
        status: str,
        payload: Any,
        observed_at: float,
        expires_at: float,
        error_type: str | None = None,
    ) -> None:
        encoded = json.dumps(payload, separators=(",", ":")) if payload is not None else None
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO cache
                   (cache_key, status, payload, observed_at, expires_at, error_type)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       status=excluded.status,
                       payload=excluded.payload,
                       observed_at=excluded.observed_at,
                       expires_at=excluded.expires_at,
                       error_type=excluded.error_type""",
                (key, status, encoded, observed_at, expires_at, error_type),
            )

    def _open_circuit(self, exc: BaseException) -> None:
        now = self._epoch()
        retry_after = _retry_after(exc) or 0.0
        lockout = max(CIRCUIT_LOCKOUT_SECONDS, min(retry_after, 60 * 60))
        with self._connect() as connection:
            connection.execute(
                "UPDATE governor SET circuit_until = ?, circuit_reason = ? WHERE singleton = 1",
                (now + lockout, "RATE_LIMITED"),
            )
        self.metrics["circuit_opened"] += 1

    @staticmethod
    def _window_wait(rows: list[sqlite3.Row], now: float, limit: int) -> float:
        if len(rows) < limit:
            return 0.0
        return max(0.0, float(rows[-limit]["requested_at"]) + 60.0 - now)

    def _reserve(
        self,
        kind: str,
        *,
        priority: int,
        deadline_seconds: float,
    ) -> None:
        deadline = self._epoch() + max(0.0, deadline_seconds)
        while True:
            now = self._epoch()
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                governor = connection.execute(
                    "SELECT * FROM governor WHERE singleton = 1"
                ).fetchone()
                assert governor is not None
                circuit_until = float(governor["circuit_until"])
                if now < circuit_until:
                    connection.rollback()
                    self.metrics["requests_avoided"] += 1
                    raise PMUSCircuitOpen(
                        f"PMUS acquisition circuit open for {circuit_until - now:.1f}s"
                    )

                tokens = min(
                    TOKEN_CAPACITY,
                    float(governor["tokens"])
                    + max(0.0, now - float(governor["last_refill"]))
                    * TOKEN_REFILL_PER_SECOND,
                )
                connection.execute("DELETE FROM request_log WHERE requested_at <= ?", (now - 60.0,))
                all_rows = connection.execute(
                    "SELECT requested_at FROM request_log ORDER BY requested_at"
                ).fetchall()
                book_rows = connection.execute(
                    "SELECT requested_at FROM request_log WHERE kind = 'book' ORDER BY requested_at"
                ).fetchall()
                if (
                    kind == "book"
                    and priority < PRIORITY_CONFIRMATION
                    and len(book_rows)
                    >= BOOK_REQUESTS_PER_MINUTE - RESERVED_CONFIRMATION_BOOKS
                ):
                    connection.execute(
                        "UPDATE governor SET tokens = ?, last_refill = ? WHERE singleton = 1",
                        (tokens, now),
                    )
                    connection.commit()
                    self.metrics["requests_avoided"] += 1
                    raise PMUSAcquisitionUnavailable(
                        "PMUS book capacity reserved for BUY confirmation"
                    )
                if (
                    priority < PRIORITY_CONFIRMATION
                    and len(all_rows)
                    >= TOTAL_REQUESTS_PER_MINUTE - RESERVED_CONFIRMATION_BOOKS
                ):
                    connection.execute(
                        "UPDATE governor SET tokens = ?, last_refill = ? WHERE singleton = 1",
                        (tokens, now),
                    )
                    connection.commit()
                    self.metrics["requests_avoided"] += 1
                    raise PMUSAcquisitionUnavailable(
                        "PMUS total capacity reserved for BUY confirmation"
                    )
                required_tokens = (
                    1.0 if priority >= PRIORITY_CONFIRMATION else 2.0
                )
                waits = [
                    max(
                        0.0,
                        (required_tokens - tokens) / TOKEN_REFILL_PER_SECOND,
                    )
                ]
                waits.append(self._window_wait(all_rows, now, TOTAL_REQUESTS_PER_MINUTE))
                if kind == "book":
                    waits.append(self._window_wait(book_rows, now, BOOK_REQUESTS_PER_MINUTE))
                wait = max(waits)
                if wait <= 1e-9:
                    connection.execute(
                        "UPDATE governor SET tokens = ?, last_refill = ? WHERE singleton = 1",
                        (tokens - 1.0, now),
                    )
                    connection.execute(
                        "INSERT INTO request_log VALUES (?, ?, ?, ?)",
                        (now, kind, self.lane, priority),
                    )
                    connection.commit()
                    return
                connection.execute(
                    "UPDATE governor SET tokens = ?, last_refill = ? WHERE singleton = 1",
                    (tokens, now),
                )
                connection.commit()
            if now + wait > deadline:
                self.metrics["requests_avoided"] += 1
                raise PMUSAcquisitionUnavailable(
                    f"PMUS {kind} request deferred beyond decision deadline"
                )
            self.metrics["governor_waits"] += 1
            self.sleeper(wait)

    def _provider_call(
        self,
        kind: str,
        request: Callable[[], Any],
        *,
        priority: int,
        deadline_seconds: float = REQUEST_DEADLINE_SECONDS,
    ) -> Any:
        self._reserve(
            kind, priority=priority, deadline_seconds=deadline_seconds
        )
        self.metrics[f"{kind}_requests"] += 1
        try:
            return request()
        except Exception as exc:
            if _is_rate_limit(exc):
                self._open_circuit(exc)
                raise PMUSCircuitOpen(
                    f"PMUS rate limit opened acquisition circuit during {kind}"
                ) from exc
            raise

    def discover(
        self,
        cache_key: str,
        request: Callable[[], list[dict[str, Any]]],
    ) -> list[dict[str, Any]]:
        key = f"discovery:{cache_key}"
        with self._singleflight(key):
            now = self._epoch()
            cached = self._cache_get(key)
            if cached is not None and now < float(cached["expires_at"]):
                self.metrics["discovery_cache_hits"] += 1
                self.metrics["requests_avoided"] += 1
                if cached["status"] != "OK":
                    raise PMUSAcquisitionUnavailable(
                        f"cached PMUS discovery failure: {cached['error_type']}"
                    )
                payload = json.loads(cached["payload"] or "[]")
                return payload if isinstance(payload, list) else []
            try:
                rows = self._provider_call(
                    "discovery", request, priority=PRIORITY_DISCOVERY
                )
                if not isinstance(rows, list):
                    raise TypeError("PMUS discovery did not return a list")
            except Exception as exc:
                self._cache_put(
                    key,
                    status="FAILED",
                    payload=None,
                    observed_at=now,
                    expires_at=now + NEGATIVE_TTL_SECONDS,
                    error_type=type(exc).__name__,
                )
                raise
            expires = now + (DISCOVERY_TTL_SECONDS if rows else NEGATIVE_TTL_SECONDS)
            self._cache_put(
                key,
                status="OK",
                payload=rows,
                observed_at=now,
                expires_at=expires,
            )
            return rows

    def book(
        self,
        slug: str,
        request: Callable[[], dict[str, Any]],
        *,
        fair_probability: float,
        calibration_edge: float = 0.0,
    ) -> BookAcquisition:
        self.metrics["candidates_considered"] += 1
        key = f"book:{slug}"
        with self._singleflight(key):
            now = self._epoch()
            cached = self._cache_get(key)
            if cached is not None and cached["status"] != "OK" and now < float(cached["expires_at"]):
                self.metrics["negative_cache_hits"] += 1
                self.metrics["requests_avoided"] += 1
                raise PMUSAcquisitionUnavailable(
                    f"cached PMUS book failure: {cached['error_type']}"
                )

            cached_book: dict[str, Any] | None = None
            cached_at: float | None = None
            if cached is not None and cached["status"] == "OK":
                payload = json.loads(cached["payload"] or "{}")
                if isinstance(payload, dict):
                    cached_book = payload
                    cached_at = float(cached["observed_at"])
            if cached_book is not None and cached_at is not None:
                age = max(0.0, now - cached_at)
                if age <= MAX_AGE_SECONDS:
                    self.metrics["book_cache_hits"] += 1
                    self.metrics["requests_avoided"] += 1
                    return BookAcquisition(cached_book, _utc_iso(cached_at), "FRESH_CACHE")
                yes_ask = _book_ask(cached_book, slug, "YES")
                no_ask = _book_ask(cached_book, slug, "NO")
                required_edge = max(MIN_EDGE, calibration_edge)
                if yes_ask is not None and no_ask is not None:
                    best_edge = max(
                        fair_probability - yes_ask,
                        (1.0 - fair_probability) - no_ask,
                    )
                    if (
                        best_edge < required_edge - MAX_SPREAD
                        and age <= OBVIOUS_NONCANDIDATE_TTL_SECONDS
                    ):
                        self.metrics["noncandidate_cache_hits"] += 1
                        self.metrics["requests_avoided"] += 1
                        return BookAcquisition(
                            None,
                            _utc_iso(cached_at),
                            "NONCANDIDATE_CACHE",
                            avoided_reason="obvious_noncandidate_reference_still_valid",
                        )

            priority = (
                PRIORITY_CONFIRMATION
                if cached_book is not None
                else PRIORITY_CANDIDATE
            )
            lane_limit = BOOK_REQUESTS_PER_LANE
            if priority < PRIORITY_CONFIRMATION:
                lane_limit -= RESERVED_CONFIRMATION_BOOKS_PER_LANE
            if self._book_requests >= lane_limit:
                self.metrics["requests_avoided"] += 1
                return BookAcquisition(
                    None,
                    _utc_iso(cached_at) if cached_at is not None else None,
                    "LANE_BUDGET",
                    avoided_reason="per_lane_book_budget_exhausted",
                )

            self.metrics["candidates_selected_for_fresh_book"] += 1
            try:
                self._reserve(
                    "book",
                    priority=priority,
                    deadline_seconds=REQUEST_DEADLINE_SECONDS,
                )
            except PMUSAcquisitionUnavailable:
                return BookAcquisition(
                    None,
                    _utc_iso(cached_at) if cached_at is not None else None,
                    "GOVERNOR",
                    avoided_reason="request_deadline_or_budget",
                )
            self._book_requests += 1
            self.metrics["book_requests"] += 1
            observed_at = self._epoch()
            try:
                book = request()
                if not isinstance(book, dict) or not book:
                    raise ValueError("PMUS book was empty or malformed")
            except Exception as exc:
                if _is_rate_limit(exc):
                    self._open_circuit(exc)
                    wrapped = PMUSCircuitOpen(
                        "PMUS rate limit opened acquisition circuit during book"
                    )
                    self._cache_put(
                        key,
                        status="FAILED",
                        payload=None,
                        observed_at=observed_at,
                        expires_at=observed_at + NEGATIVE_TTL_SECONDS,
                        error_type=type(wrapped).__name__,
                    )
                    raise wrapped from exc
                self._cache_put(
                    key,
                    status="FAILED",
                    payload=None,
                    observed_at=observed_at,
                    expires_at=observed_at + NEGATIVE_TTL_SECONDS,
                    error_type=type(exc).__name__,
                )
                raise
            self._cache_put(
                key,
                status="OK",
                payload=book,
                observed_at=observed_at,
                expires_at=observed_at + OBVIOUS_NONCANDIDATE_TTL_SECONDS,
            )
            return BookAcquisition(book, _utc_iso(observed_at), "PROVIDER", requested=True)

    def diagnostics(self) -> dict[str, Any]:
        now = self._epoch()
        with self._connect() as connection:
            connection.execute("DELETE FROM request_log WHERE requested_at <= ?", (now - 60.0,))
            total = connection.execute("SELECT COUNT(*) FROM request_log").fetchone()[0]
            books = connection.execute(
                "SELECT COUNT(*) FROM request_log WHERE kind = 'book'"
            ).fetchone()[0]
            governor = connection.execute(
                "SELECT circuit_until, circuit_reason FROM governor WHERE singleton = 1"
            ).fetchone()
        circuit_until = float(governor["circuit_until"]) if governor else 0.0
        return {
            "lane": self.lane,
            "discovery_requests": self.metrics["discovery_requests"],
            "book_requests": self.metrics["book_requests"],
            "cache_hits": (
                self.metrics["discovery_cache_hits"]
                + self.metrics["book_cache_hits"]
                + self.metrics["noncandidate_cache_hits"]
                + self.metrics["negative_cache_hits"]
            ),
            "requests_avoided": self.metrics["requests_avoided"],
            "candidates_considered": self.metrics["candidates_considered"],
            "candidates_selected_for_fresh_book": self.metrics[
                "candidates_selected_for_fresh_book"
            ],
            "requests_last_minute": int(total),
            "book_requests_last_minute": int(books),
            "request_budget_per_minute": TOTAL_REQUESTS_PER_MINUTE,
            "book_budget_per_minute": BOOK_REQUESTS_PER_MINUTE,
            "confirmation_capacity_reserved": RESERVED_CONFIRMATION_BOOKS,
            "lane_confirmation_capacity_reserved": (
                RESERVED_CONFIRMATION_BOOKS_PER_LANE
            ),
            "circuit_open": now < circuit_until,
            "circuit_seconds_remaining": max(0.0, circuit_until - now),
            "circuit_reason": governor["circuit_reason"] if governor else None,
        }
