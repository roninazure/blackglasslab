"""Authenticated Polymarket US market-data stream for sports consumers.

This component deliberately has no REST implementation.  Callers that need an
exceptional REST fallback must route it through their existing acquisition
policy rather than turning a stream outage into an ungoverned polling loop.
"""

from __future__ import annotations

import asyncio
import copy
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from maker_spread_economics.polymarket_us import (
    PMUS_KEY_ID_ENV,
    PMUS_SECRET_KEY_ENV,
    normalize_book,
    redact_sensitive,
    token_id,
)


class PMUSMarketDataUnavailable(RuntimeError):
    """No valid fresh authenticated stream book is currently available."""


class PMUSMarketDataState(str, Enum):
    NEW = "NEW"
    CONNECTING = "CONNECTING"
    HEALTHY = "HEALTHY"
    RECOVERING = "RECOVERING"
    FAILED = "FAILED"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class PMUSStreamBook:
    book: dict[str, Any]
    observed_at: str
    observed_monotonic: float


@dataclass(frozen=True)
class _CachedBook:
    book: dict[str, Any]
    observed_at: str
    observed_monotonic: float


class _StopRequested(Exception):
    pass


Waiter = Callable[[threading.Event, float], bool]


class PMUSMarketDataStream:
    """One authenticated multi-market websocket with a fresh normalized cache."""

    def __init__(
        self,
        websocket_client: Any | None,
        market_slugs: list[str],
        *,
        stale_seconds: float = 60.0,
        max_reconnect_attempts: int = 5,
        backoff_initial_seconds: float = 1.0,
        backoff_cap_seconds: float = 30.0,
        connect_timeout_seconds: float = 8.0,
        close_timeout_seconds: float = 1.0,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] | None = None,
        waiter: Waiter | None = None,
        owns_client: bool = False,
    ) -> None:
        slugs = sorted(set(market_slugs))
        if not slugs or len(slugs) > 100 or any(not slug for slug in slugs):
            raise ValueError("PMUS market-data subscriptions require 1-100 slugs")
        if (
            stale_seconds <= 0
            or connect_timeout_seconds <= 0
            or close_timeout_seconds <= 0
        ):
            raise ValueError("PMUS market-data timeouts must be positive")
        if max_reconnect_attempts < 0:
            raise ValueError("PMUS reconnect attempts cannot be negative")
        if backoff_initial_seconds < 0 or backoff_cap_seconds < 0:
            raise ValueError("PMUS reconnect backoff cannot be negative")

        self.websocket_client = websocket_client
        self.market_slugs = slugs
        self.stale_seconds = stale_seconds
        self.max_reconnect_attempts = max_reconnect_attempts
        self.backoff_initial_seconds = backoff_initial_seconds
        self.backoff_cap_seconds = backoff_cap_seconds
        self.connect_timeout_seconds = connect_timeout_seconds
        self.close_timeout_seconds = close_timeout_seconds
        self._monotonic = monotonic
        self._utcnow = utcnow or (lambda: datetime.now(UTC))
        self._waiter = waiter or (lambda event, delay: event.wait(delay))
        self._owns_client = owns_client

        self._lock = threading.RLock()
        self._state = PMUSMarketDataState.NEW
        self._error: str | None = None
        self._terminal_protocol_failure = False
        self._cache: dict[str, _CachedBook] = {}
        self._last_message_monotonic = 0.0
        self._connection_attempts = 0
        self._reconnects = 0
        self._backoff_history: list[float] = []
        self._stop_thread = threading.Event()
        self._ready_event = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_async: asyncio.Event | None = None
        self._cycle_end_async: asyncio.Event | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(
        cls,
        market_slugs: list[str],
        *,
        environ: Mapping[str, str] | None = None,
        client_factory: Callable[..., Any] | None = None,
        **kwargs: Any,
    ) -> PMUSMarketDataStream:
        environment = os.environ if environ is None else environ
        missing = [
            name
            for name in (PMUS_KEY_ID_ENV, PMUS_SECRET_KEY_ENV)
            if not environment.get(name)
        ]
        if missing:
            raise PMUSMarketDataUnavailable(
                "authenticated Polymarket US market data is unavailable; missing: "
                + ", ".join(missing)
            )
        if client_factory is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:
                raise PMUSMarketDataUnavailable(
                    "polymarket-us is required for authenticated market data"
                ) from exc
            client_factory = PolymarketUS
        try:
            client = client_factory(
                key_id=environment[PMUS_KEY_ID_ENV],
                secret_key=environment[PMUS_SECRET_KEY_ENV],
                timeout=8.0,
            )
        except Exception as exc:
            raise PMUSMarketDataUnavailable(
                "authenticated Polymarket US client construction failed "
                f"({type(exc).__name__})"
            ) from exc
        return cls(client, market_slugs, owns_client=True, **kwargs)

    @property
    def state(self) -> PMUSMarketDataState:
        with self._lock:
            return self._state

    @property
    def failed(self) -> bool:
        return self.state == PMUSMarketDataState.FAILED

    @property
    def error(self) -> str | None:
        with self._lock:
            return self._error

    def _safe_error(self, exc: BaseException) -> str:
        return f"{type(exc).__name__}: {redact_sensitive(exc)}"[:320]

    def _set_state(
        self, state: PMUSMarketDataState, error: BaseException | str | None = None
    ) -> None:
        with self._lock:
            self._state = state
            if error is not None:
                self._error = (
                    self._safe_error(error)
                    if isinstance(error, BaseException)
                    else str(error)[:320]
                )
            if state in {PMUSMarketDataState.FAILED, PMUSMarketDataState.STOPPED}:
                self._ready_event.set()

    def _terminal_fail(self, exc: BaseException) -> None:
        with self._lock:
            self._terminal_protocol_failure = True
        self._set_state(PMUSMarketDataState.FAILED, exc)
        loop = self._loop
        event = self._cycle_end_async
        if loop is not None and event is not None:
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                pass

    def _apply(self, message: dict[str, Any]) -> None:
        market_data = message.get("marketData", {}) if isinstance(message, dict) else {}
        slug = str(market_data.get("marketSlug") or "")
        if slug not in self.market_slugs:
            raise PMUSMarketDataUnavailable("unexpected Polymarket US websocket market")
        normalized = normalize_book(message, expected_slug=slug)
        observed_monotonic = self._monotonic()
        observed_datetime = self._utcnow()
        if observed_datetime.tzinfo is None:
            raise ValueError("PMUS market-data clock must be timezone-aware")
        observed_at = observed_datetime.astimezone(UTC).isoformat()
        for outcome in ("YES", "NO"):
            normalized[token_id(slug, outcome)]["book_observed_monotonic"] = (
                observed_monotonic
            )
        cached = _CachedBook(
            book=copy.deepcopy(normalized),
            observed_at=observed_at,
            observed_monotonic=observed_monotonic,
        )
        with self._lock:
            self._cache[slug] = cached
            self._last_message_monotonic = observed_monotonic
            if all(item in self._cache for item in self.market_slugs):
                self._ready_event.set()

    async def _operation_or_stop(self, awaitable: Any) -> Any:
        assert self._stop_async is not None
        operation = asyncio.ensure_future(awaitable)
        stopped = asyncio.create_task(self._stop_async.wait())
        done, pending = await asyncio.wait(
            {operation, stopped},
            timeout=self.connect_timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if stopped in done and stopped.result():
            if operation not in done:
                operation.cancel()
                await asyncio.gather(operation, return_exceptions=True)
            raise _StopRequested
        if operation not in done:
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)
            raise TimeoutError("Polymarket US websocket operation timed out")
        return operation.result()

    def _freshness_wait_seconds(self, cycle_started: float) -> float:
        now = self._monotonic()
        with self._lock:
            if any(slug not in self._cache for slug in self.market_slugs):
                deadline = cycle_started + self.stale_seconds
            else:
                deadline = min(
                    self._cache[slug].observed_monotonic + self.stale_seconds
                    for slug in self.market_slugs
                )
        return max(0.0, deadline - now)

    async def _wait_for_cycle_signal(
        self, activity: asyncio.Event, timeout_seconds: float
    ) -> str:
        assert self._stop_async is not None
        assert self._cycle_end_async is not None
        tasks = {
            "stop": asyncio.create_task(self._stop_async.wait()),
            "end": asyncio.create_task(self._cycle_end_async.wait()),
            "activity": asyncio.create_task(activity.wait()),
        }
        done, pending = await asyncio.wait(
            set(tasks.values()),
            timeout=timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if not done:
            return "stale"
        for label in ("stop", "end", "activity"):
            if tasks[label] in done:
                return label
        raise AssertionError("unreachable PMUS websocket signal state")

    async def _connection_cycle(self) -> tuple[BaseException | None, bool]:
        loop = asyncio.get_running_loop()
        self._loop = loop
        self._stop_async = asyncio.Event()
        self._cycle_end_async = asyncio.Event()
        if self._stop_thread.is_set():
            self._stop_async.set()
            return None, False
        activity = asyncio.Event()
        cycle_started = self._monotonic()
        established_complete = False
        cycle_error: BaseException | None = None
        websocket: Any | None = None
        closing = False

        def signal_error(error: BaseException) -> None:
            nonlocal cycle_error
            if closing:
                return
            cycle_error = (
                error if isinstance(error, BaseException) else RuntimeError(str(error))
            )
            event = self._cycle_end_async
            if event is not None:
                loop.call_soon_threadsafe(event.set)

        def on_market_data(message: dict[str, Any]) -> None:
            nonlocal established_complete
            if closing:
                return
            try:
                self._apply(message)
                with self._lock:
                    established_complete = all(
                        slug in self._cache
                        and 0
                        <= self._monotonic() - self._cache[slug].observed_monotonic
                        <= self.stale_seconds
                        for slug in self.market_slugs
                    )
                loop.call_soon_threadsafe(activity.set)
            except Exception as exc:  # noqa: BLE001 - schema drift fails closed
                self._terminal_fail(exc)

        def on_heartbeat() -> None:
            if closing:
                return
            with self._lock:
                self._last_message_monotonic = self._monotonic()

        def on_error(error: BaseException) -> None:
            signal_error(error)

        def on_close() -> None:
            if not closing and not self._stop_thread.is_set():
                signal_error(RuntimeError("Polymarket US market websocket closed"))

        try:
            websocket = self.websocket_client.ws.markets()
            websocket.on("market_data", on_market_data)
            websocket.on("heartbeat", on_heartbeat)
            websocket.on("error", on_error)
            websocket.on("close", on_close)
            await self._operation_or_stop(websocket.connect())
            await self._operation_or_stop(
                websocket.subscribe_market_data(
                    "parallax-sports-books", self.market_slugs
                )
            )
            with self._lock:
                if self._terminal_protocol_failure:
                    return RuntimeError(
                        self._error or "Polymarket US websocket protocol failure"
                    ), established_complete
            self._set_state(PMUSMarketDataState.HEALTHY)
            while not self._stop_thread.is_set():
                signal = await self._wait_for_cycle_signal(
                    activity, self._freshness_wait_seconds(cycle_started)
                )
                if signal == "activity":
                    activity.clear()
                    continue
                if signal == "stop":
                    raise _StopRequested
                if signal == "stale":
                    cycle_error = PMUSMarketDataUnavailable(
                        "Polymarket US stream produced no fresh books before stale deadline"
                    )
                break
        except _StopRequested:
            return None, established_complete
        except Exception as exc:  # noqa: BLE001 - recovered by bounded outer loop
            cycle_error = exc
        finally:
            closing = True
            if websocket is not None:
                try:
                    await asyncio.wait_for(
                        websocket.close(), timeout=self.close_timeout_seconds
                    )
                except Exception:  # noqa: BLE001,S110 - shutdown remains bounded
                    pass
        return cycle_error, established_complete

    def _run(self) -> None:
        consecutive_failures = 0
        while not self._stop_thread.is_set():
            with self._lock:
                self._connection_attempts += 1
            self._set_state(PMUSMarketDataState.CONNECTING)
            error, established_complete = asyncio.run(self._connection_cycle())
            if self._stop_thread.is_set():
                break
            with self._lock:
                if self._terminal_protocol_failure:
                    break
            if established_complete:
                consecutive_failures = 0
            consecutive_failures += 1
            if consecutive_failures > self.max_reconnect_attempts:
                self._set_state(
                    PMUSMarketDataState.FAILED,
                    error or RuntimeError("Polymarket US websocket recovery exhausted"),
                )
                break
            delay = min(
                self.backoff_cap_seconds,
                self.backoff_initial_seconds * (2 ** (consecutive_failures - 1)),
            )
            with self._lock:
                self._reconnects += 1
                self._backoff_history.append(delay)
            self._set_state(
                PMUSMarketDataState.RECOVERING,
                error or RuntimeError("Polymarket US websocket disconnected"),
            )
            if self._waiter(self._stop_thread, delay):
                break
        if self._stop_thread.is_set():
            self._set_state(PMUSMarketDataState.STOPPED)

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self._state != PMUSMarketDataState.NEW:
                raise RuntimeError("PMUS market-data stream was already started")
            if self.websocket_client is None:
                self._state = PMUSMarketDataState.FAILED
                self._error = "authenticated Polymarket US websocket client unavailable"
                self._ready_event.set()
                return
            self._thread = threading.Thread(
                target=self._run,
                name="parallax-pmus-sports-market-data",
                daemon=True,
            )
            self._thread.start()

    def ready(self) -> bool:
        now = self._monotonic()
        with self._lock:
            if self._state != PMUSMarketDataState.HEALTHY:
                return False
            return all(
                slug in self._cache
                and 0
                <= now - self._cache[slug].observed_monotonic
                <= self.stale_seconds
                for slug in self.market_slugs
            )

    def wait_ready(self, timeout_seconds: float) -> bool:
        deadline = self._monotonic() + max(0.0, timeout_seconds)
        while True:
            if self.ready():
                return True
            if self.failed or self.state == PMUSMarketDataState.STOPPED:
                return False
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                return False
            self._ready_event.clear()
            if self.ready():
                return True
            self._ready_event.wait(min(remaining, 0.05))

    def book(self, slug: str) -> PMUSStreamBook:
        now = self._monotonic()
        with self._lock:
            if slug not in self.market_slugs:
                raise PMUSMarketDataUnavailable(
                    "PMUS stream book requested for unsubscribed market"
                )
            if self._state in {
                PMUSMarketDataState.FAILED,
                PMUSMarketDataState.STOPPED,
            }:
                raise PMUSMarketDataUnavailable(
                    self._error or "Polymarket US market-data stream failed"
                )
            cached = self._cache.get(slug)
            if cached is None:
                raise PMUSMarketDataUnavailable("missing streamed Polymarket US book")
            age = now - cached.observed_monotonic
            if age < 0 or age > self.stale_seconds:
                raise PMUSMarketDataUnavailable("stale streamed Polymarket US book")
            return PMUSStreamBook(
                book=copy.deepcopy(cached.book),
                observed_at=cached.observed_at,
                observed_monotonic=cached.observed_monotonic,
            )

    def stop(self) -> bool:
        self._stop_thread.set()
        loop = self._loop
        for event in (self._stop_async, self._cycle_end_async):
            if loop is not None and event is not None:
                try:
                    loop.call_soon_threadsafe(event.set)
                except RuntimeError:
                    pass
        thread = self._thread
        if thread is not None:
            thread.join(
                timeout=self.connect_timeout_seconds + self.close_timeout_seconds + 0.5
            )
        stopped = thread is None or not thread.is_alive()
        if stopped:
            self._set_state(PMUSMarketDataState.STOPPED)
        else:
            self._set_state(
                PMUSMarketDataState.FAILED,
                "Polymarket US market-data worker did not stop within bound",
            )
        if self._owns_client:
            close = getattr(self.websocket_client, "close", None)
            if callable(close):

                def close_client() -> None:
                    try:
                        close()
                    except Exception:  # noqa: BLE001,S110 - shutdown remains bounded
                        pass

                closer = threading.Thread(target=close_client, daemon=True)
                closer.start()
                closer.join(timeout=self.close_timeout_seconds)
                if closer.is_alive():
                    stopped = False
                    self._set_state(
                        PMUSMarketDataState.FAILED,
                        "Polymarket US SDK client did not close within bound",
                    )
        return stopped

    def diagnostics(self) -> dict[str, Any]:
        now = self._monotonic()
        with self._lock:
            fresh = sum(
                0 <= now - cached.observed_monotonic <= self.stale_seconds
                for cached in self._cache.values()
            )
            readiness = (
                "FULL"
                if fresh == len(self.market_slugs)
                else "PARTIAL"
                if fresh > 0
                else "NONE"
            )
            return {
                "state": self._state.value,
                "markets_requested": len(self.market_slugs),
                "fresh_books": fresh,
                "readiness": readiness,
                "connection_attempts": self._connection_attempts,
                "reconnects": self._reconnects,
                "backoff_seconds": list(self._backoff_history),
                "last_message_monotonic": self._last_message_monotonic,
                "error": self._error,
                "rest_polling": False,
            }

