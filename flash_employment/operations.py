from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Self

EXPECTED_EVENT_DATE = date(2026, 9, 4)
SOURCE_PHASE_OFFSETS_SECONDS = {
    "rss": 0.0,
    "summary": 0.33,
    "table_b1": 0.66,
}
MIN_SOURCE_POLL_SECONDS = 1.0
REQUIRED_STATIC_GATES = (
    "FLASH_CONTACT",
    "EVENT_DATE",
    "POLYMARKET_EVENT",
    "RESOLUTION_RULES",
    "PAYROLL_BRACKETS",
    "TOKEN_MAP",
    "FEE_METADATA",
    "KNOWN_CLAIM_MAPPINGS",
    "TRIAL_DATABASE",
    "HIGH_RESOLUTION_CLOCKS",
    "DUPLICATE_RUNNER",
    "PAPER_ONLY_MODE",
    "NO_ORDER_AUTH_WALLET_PATH",
)


class ReadinessFailure(RuntimeError):
    """One or more mandatory FLASH arm gates failed."""


class DuplicateTrialError(ReadinessFailure):
    """Another process owns the same event/date advisory lock."""


class TrialLock:
    """Small process-lifetime advisory lock for one FLASH event/date."""

    def __init__(self, event_slug: str, event_date: date, *, root: Path = Path("/tmp")):
        self.identity = f"{event_slug}:{event_date.isoformat()}"
        digest = hashlib.sha256(self.identity.encode()).hexdigest()[:16]
        self.path = root / f"parallax-flash-{event_date:%Y%m%d}-{digest}.lock"
        self._handle: object | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "owner details unavailable"
            handle.close()
            raise DuplicateTrialError(
                f"duplicate FLASH runner for this event/date ({owner})"
            ) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "event": self.identity,
                    "acquired_utc": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            )
        )
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        if self._handle is None:
            return
        handle = self._handle
        self._handle = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass(frozen=True)
class GateResult:
    name: str
    passed: bool
    detail: str


def clocks_function() -> bool:
    wall_before = time.time_ns()
    mono_before = time.monotonic_ns()
    wall_after = time.time_ns()
    mono_after = time.monotonic_ns()
    return wall_after >= wall_before and mono_after >= mono_before


def coarse_http_clock_delta_ms(http_date: str | None, local_wall_ns: int) -> float | None:
    """Advisory local-minus-HTTP-Date delta; never a latency measurement."""
    if not http_date:
        return None
    try:
        remote = parsedate_to_datetime(http_date)
    except (TypeError, ValueError, OverflowError):
        return None
    if remote.tzinfo is None:
        remote = remote.replace(tzinfo=UTC)
    return local_wall_ns / 1e6 - remote.timestamp() * 1_000.0


def source_poll_sleep_seconds(
    *, request_started_mono_ns: int, now_mono_ns: int, after_release_seconds: float
) -> float:
    interval = MIN_SOURCE_POLL_SECONDS if after_release_seconds < 30 else 5.0
    request_age = max(0.0, (now_mono_ns - request_started_mono_ns) / 1e9)
    return max(0.0, interval - request_age)
