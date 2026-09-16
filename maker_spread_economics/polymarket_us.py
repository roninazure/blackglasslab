from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .live_engine import OrderIntent, ReconciliationError, SafetyStop

PMUS_KEY_ID_ENV = "PARALLAX_PMUS_KEY_ID"
PMUS_SECRET_KEY_ENV = "PARALLAX_PMUS_SECRET_KEY"
PMUS_DISCOVERY_MAX_ATTEMPTS = 3
PMUS_DISCOVERY_BACKOFF_SECONDS = (0.1, 0.2)


class PolymarketUSRateLimit(SafetyStop):
    """Authenticated US REST traffic is temporarily unsafe to send."""


class PolymarketUSOrderRejected(SafetyStop):
    """A bounded, credential-free Polymarket US order rejection."""

    def __init__(self, details: dict[str, Any]) -> None:
        self.details = details
        super().__init__(
            f"status={details.get('status') or 'UNKNOWN'}; "
            f"reason={details.get('reason') or 'unknown venue rejection'}"
        )


class PolymarketUSDiscoveryFailure(SafetyStop):
    """Fail-closed discovery failure with bounded retry metadata."""

    def __init__(self, message: str, *, attempts: int, underlying_error: Exception) -> None:
        self.attempts = attempts
        self.underlying_error = underlying_error
        super().__init__(message)


def _retryable_discovery_error(exc: BaseException) -> bool:
    """Recognize only transport/timeouts, including SDK-wrapped causes."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        name = type(current).__name__.lower()
        module = type(current).__module__.lower()
        if "timeout" in name or "timeout" in module or "network" in name or "requesterror" in name:
            return True
        current = current.__cause__ or current.__context__
    return False


class RequestMeter:
    def __init__(self) -> None:
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def record(self) -> None:
        with self._lock:
            self._timestamps.append(time.monotonic())
            self._trim(time.monotonic())

    def per_minute(self) -> int:
        with self._lock:
            self._trim(time.monotonic())
            return len(self._timestamps)

    def _trim(self, now: float) -> None:
        while self._timestamps and self._timestamps[0] < now - 60.0:
            self._timestamps.popleft()


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {})
    value = headers.get("Retry-After") if headers else None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _is_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    body = str(getattr(exc, "body", ""))
    text = f"{exc} {body}".lower()
    return status == 429 or "1015" in text or "rate limit" in text or "rate-limit" in text


class AuthenticatedRESTGate:
    """Single conservative budget and lockout for every authenticated REST call."""

    def __init__(self, *, minimum_interval_seconds: float = 5.0) -> None:
        self.minimum_interval_seconds = minimum_interval_seconds
        self.meter = RequestMeter()
        self._lock = threading.Lock()
        self._next_request_at = 0.0
        self._locked_until = 0.0
        self._rate_limit_events = 0
        self._backoff_seconds = 0.0

    def call(
        self, operation: str, request: Any, *, cancellation_retry: bool = False
    ) -> Any:
        attempts = 2 if cancellation_retry else 1
        for attempt in range(attempts):
            while True:
                with self._lock:
                    now = time.monotonic()
                    if now < self._locked_until:
                        raise PolymarketUSRateLimit(
                            f"authenticated REST lockout during {operation}; "
                            f"retry in {self._locked_until - now:.1f}s"
                        )
                    delay = self._next_request_at - now
                    if delay <= 0:
                        self._next_request_at = now + self.minimum_interval_seconds
                        break
                # Deliberately serialize, rather than burst, authenticated calls.
                time.sleep(delay)
            self.meter.record()
            try:
                return request()
            except Exception as exc:
                if not _is_rate_limit(exc):
                    raise
                retry_after = _retry_after_seconds(exc)
                with self._lock:
                    self._rate_limit_events += 1
                    exponential = min(60.0, float(2 ** min(self._rate_limit_events, 5)))
                    self._backoff_seconds = max(
                        retry_after if retry_after is not None else 0.0,
                        exponential + random.uniform(0.0, 0.5),
                    )
                    self._locked_until = time.monotonic() + self._backoff_seconds
                if cancellation_retry and attempt == 0 and self._backoff_seconds <= 15.0:
                    time.sleep(self._backoff_seconds)
                    continue
                raise PolymarketUSRateLimit(
                    f"authenticated REST rate limit during {operation}; "
                    f"backoff={self._backoff_seconds:.1f}s"
                ) from exc
        raise AssertionError("unreachable authenticated REST retry state")

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "authenticated_rest_requests_per_minute": self.meter.per_minute(),
                "rate_limit_events": self._rate_limit_events,
                "backoff_state": max(0.0, self._locked_until - time.monotonic()),
            }


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"invalid Polymarket US {name}") from exc
    if not math.isfinite(result):
        raise ReconciliationError(f"non-finite Polymarket US {name}")
    return result


def _amount(value: Any, *, name: str, default: float | None = None) -> float:
    if value in (None, ""):
        if default is not None:
            return default
        raise ReconciliationError(f"missing Polymarket US {name}")
    if isinstance(value, dict):
        currency = str(value.get("currency") or "USD").upper()
        if currency != "USD":
            raise ReconciliationError(f"unexpected Polymarket US {name} currency")
    return _number(value, name=name)


def token_id(market_slug: str, outcome: str) -> str:
    outcome = outcome.upper()
    if not market_slug or outcome not in {"YES", "NO"}:
        raise ReconciliationError("invalid Polymarket US market token")
    return f"{market_slug}::{outcome}"


def split_token_id(value: str) -> tuple[str, str]:
    try:
        slug, outcome = value.rsplit("::", 1)
    except ValueError as exc:
        raise ReconciliationError("invalid Polymarket US internal token") from exc
    if not slug or outcome not in {"YES", "NO"}:
        raise ReconciliationError("invalid Polymarket US internal token")
    return slug, outcome


def redact_sensitive(value: object) -> str:
    text = str(value)
    for name in (PMUS_SECRET_KEY_ENV, PMUS_KEY_ID_ENV):
        secret = os.environ.get(name)
        if secret:
            text = text.replace(secret, "<redacted>")
    text = re.sub(
        r"(?i)(authorization|x-pm-signature|secret(?:_key)?|api[_-]?key)"
        r"\s*[:=]\s*[^\s,;]+",
        r"\1=<redacted>",
        text,
    )
    return text


def _bounded_error_value(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key)[:64]: _bounded_error_value(item)
            for key, item in list(value.items())[:8]
        }
    if isinstance(value, (list, tuple)):
        return [_bounded_error_value(item) for item in list(value)[:8]]
    if isinstance(value, str):
        return " ".join(redact_sensitive(value).split())[:240]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return redact_sensitive(value)[:240]


def extract_sdk_error(exc: Exception) -> dict[str, Any]:
    """Extract bounded SDK error fields without headers, signatures, or credentials."""
    request = getattr(exc, "request", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        reason_value = body.get("message") or body.get("error") or body.get("detail")
    else:
        reason_value = body
    reason = redact_sensitive(
        reason_value or getattr(exc, "message", None) or str(exc)
    )
    reason = " ".join(reason.split())[:240] or "unknown venue rejection"
    safe_body: object
    if isinstance(body, dict):
        safe_body = _bounded_error_value(
            {
                key: body[key]
                for key in ("code", "message", "error", "detail", "details")
                if key in body
            }
        )
    elif body is None:
        safe_body = None
    else:
        safe_body = " ".join(redact_sensitive(body).split())[:240]
    return {
        "exception_type": type(exc).__name__,
        "status": getattr(exc, "status_code", None),
        "reason": reason,
        "body": safe_body,
        "method": getattr(request, "method", None),
        "endpoint": str(getattr(request, "url", "")).split("?", 1)[0] or None,
    }


def format_order_rejected(intent: OrderIntent, details: dict[str, Any]) -> str:
    return (
        "ORDER_REJECTED "
        f"market={intent.market_id} side={intent.side} price={intent.price:g} "
        f"qty={intent.size_shares:g} notional={intent.notional_usd:g} "
        f"status={details.get('status') or 'UNKNOWN'} "
        f"reason={details.get('reason') or 'unknown venue rejection'}"
    )


def _on_increment(value: float, increment: float) -> bool:
    units = value / increment
    return abs(units - round(units)) <= 1e-8


def normalize_market_page(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("markets"), list):
        raise ReconciliationError("Polymarket US market response omitted markets")
    markets: list[dict[str, Any]] = []
    for raw in payload["markets"]:
        if not isinstance(raw, dict):
            raise ReconciliationError("malformed Polymarket US market")
        slug = str(raw.get("slug") or "")
        market_id = str(raw.get("id") or slug)
        if not slug or not market_id:
            raise ReconciliationError("Polymarket US market omitted ID/slug")
        sides = raw.get("marketSides")
        side_rows = sides if isinstance(sides, list) else []
        tradable = any(
            isinstance(side, dict) and side.get("tradable") is True
            for side in side_rows
        ) if side_rows else True
        markets.append(
            {
                "id": market_id,
                "slug": slug,
                "event_id": str(raw.get("eventSlug") or market_id),
                "question": str(raw.get("question") or raw.get("title") or slug),
                "active": raw.get("active") is True,
                "closed": raw.get("closed") is True,
                "accepting_orders": (
                    raw.get("status") in (None, "", "MARKET_STATUS_OPEN")
                    and raw.get("ep3Status") in (None, "", "OPEN")
                    and tradable
                ),
                "tick_size": _number(
                    raw.get("orderPriceMinTickSize", 0.01), name="tick size"
                ),
                "minimum_trade_quantity": _number(
                    raw.get("minimumTradeQty", 1.0), name="minimum trade quantity"
                ),
                "volume": _number(raw.get("volume", 0.0), name="market volume"),
                "liquidity": _number(
                    raw.get("liquidity", 0.0), name="market liquidity"
                ),
                "raw": raw,
            }
        )
    return markets


def normalize_book(payload: Any, *, expected_slug: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ReconciliationError("malformed Polymarket US book response")
    book = payload.get("marketData", payload)
    if not isinstance(book, dict):
        raise ReconciliationError("Polymarket US book response omitted marketData")
    slug = str(book.get("marketSlug") or "")
    if slug != expected_slug:
        raise ReconciliationError("Polymarket US book market mismatch")
    state = str(book.get("state") or "")
    if state != "MARKET_STATE_OPEN":
        raise ReconciliationError(f"Polymarket US market is not open: {state or 'UNKNOWN'}")

    def levels(name: str) -> list[tuple[float, float]]:
        rows = book.get(name)
        if not isinstance(rows, list):
            raise ReconciliationError(f"Polymarket US book omitted {name}")
        result: list[tuple[float, float]] = []
        for row in rows:
            if not isinstance(row, dict):
                raise ReconciliationError("malformed Polymarket US book level")
            price = _amount(row.get("px"), name="book price")
            quantity = _number(row.get("qty"), name="book quantity")
            if not 0 < price < 1 or quantity <= 0:
                raise ReconciliationError("invalid Polymarket US book level economics")
            result.append((price, quantity))
        return result

    bids = levels("bids")
    asks = levels("offers")
    if not bids or not asks:
        raise ReconciliationError("Polymarket US book is not two-sided")
    yes_bid = max(bids, key=lambda row: row[0])
    yes_ask = min(asks, key=lambda row: row[0])
    if yes_bid[0] >= yes_ask[0]:
        raise ReconciliationError("crossed Polymarket US book")
    observed = time.monotonic()
    return {
        token_id(slug, "YES"): {
            "best_bid": yes_bid[0],
            "best_ask": yes_ask[0],
            "bid_size_shares": yes_bid[1],
            "ask_size_shares": yes_ask[1],
            "book_observed_monotonic": observed,
        },
        token_id(slug, "NO"): {
            "best_bid": 1.0 - yes_ask[0],
            "best_ask": 1.0 - yes_bid[0],
            "bid_size_shares": yes_ask[1],
            "ask_size_shares": yes_bid[1],
            "book_observed_monotonic": observed,
        },
        "transact_time": book.get("transactTime"),
        "stats": book.get("stats") if isinstance(book.get("stats"), dict) else {},
    }


def _outcome_from_intent(intent: str) -> str:
    if intent.endswith("_LONG"):
        return "YES"
    if intent.endswith("_SHORT"):
        return "NO"
    raise ReconciliationError(f"unknown Polymarket US order intent: {intent}")


def _action_from_intent(intent: str) -> str:
    if "_BUY_" in intent:
        return "BUY"
    if "_SELL_" in intent:
        return "SELL"
    raise ReconciliationError(f"unknown Polymarket US order intent: {intent}")


def normalize_order(raw: Any, *, post_only: bool = False) -> dict[str, Any]:
    if isinstance(raw, dict) and isinstance(raw.get("order"), dict):
        raw = raw["order"]
    if not isinstance(raw, dict):
        raise ReconciliationError("malformed Polymarket US order")
    order_id = str(raw.get("id") or "")
    slug = str(raw.get("marketSlug") or "")
    intent = str(raw.get("intent") or "")
    if not order_id or not slug or not intent:
        raise ReconciliationError("Polymarket US order omitted identity")
    outcome = _outcome_from_intent(intent)
    action = _action_from_intent(intent)
    long_price = _amount(raw.get("price"), name="order price")
    average = _amount(raw.get("avgPx"), name="average fill price", default=long_price)
    quantity = _number(raw.get("quantity", 0), name="order quantity")
    cumulative = _number(raw.get("cumQuantity", 0), name="cumulative fill quantity")
    leaves = _number(
        raw.get("leavesQuantity", max(0.0, quantity - cumulative)),
        name="leaves quantity",
    )
    state = str(raw.get("state") or "")
    status_by_state = {
        "ORDER_STATE_NEW": "LIVE",
        "ORDER_STATE_PARTIALLY_FILLED": "LIVE",
        "ORDER_STATE_FILLED": "FILLED",
        "ORDER_STATE_CANCELED": "CANCELLED",
        "ORDER_STATE_REJECTED": "REJECTED",
        "ORDER_STATE_EXPIRED": "EXPIRED",
        "ORDER_STATE_REPLACED": "CANCELLED",
    }
    commission = _amount(
        raw.get("commissionNotionalTotalCollected"),
        name="commission total",
        default=0.0,
    )
    maker_bps = _number(
        raw.get("makerCommissionsBasisPoints", 0), name="maker commission bps"
    )
    is_maker = post_only or maker_bps < 0
    internal_price = long_price if outcome == "YES" else 1.0 - long_price
    internal_average = average if outcome == "YES" else 1.0 - average
    return {
        "id": order_id,
        "orderID": order_id,
        "market_id": slug,
        "marketSlug": slug,
        "token_id": token_id(slug, outcome),
        "outcome": outcome,
        "side": action,
        "price": internal_price,
        "quantity": quantity,
        "size_matched": cumulative,
        "leaves_quantity": leaves,
        "status": status_by_state.get(state, state),
        "state": state,
        "avg_fill_price": internal_average,
        "fees_total_usd": 0.0 if is_maker else max(0.0, commission),
        "maker_rebate_total_usd": max(0.0, commission) if is_maker else 0.0,
        "cumulative_fill": True,
        "timestamp": raw.get("updateTime") or raw.get("insertTime") or raw.get("createTime"),
        "raw": raw,
    }


def normalize_execution(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ReconciliationError("malformed Polymarket US execution")
    order = normalize_order(raw.get("order"), post_only=raw.get("aggressor") is False)
    kind = str(raw.get("type") or "")
    quantity = _number(raw.get("lastShares", 0), name="execution quantity")
    long_price = _amount(raw.get("lastPx"), name="execution price", default=order["price"])
    price = long_price if order["outcome"] == "YES" else 1.0 - long_price
    commission = _amount(
        raw.get("commissionNotionalCollected"), name="execution commission", default=0.0
    )
    return {
        "id": str(raw.get("id") or raw.get("tradeId") or ""),
        "status": (
            "CONFIRMED"
            if kind in {"EXECUTION_TYPE_PARTIAL_FILL", "EXECUTION_TYPE_FILL"}
            else kind
        ),
        "match_time": raw.get("transactTime") or order.get("timestamp"),
        "maker_orders": [
            {
                "order_id": order["id"],
                "matched_amount": quantity,
                "price": price,
                "fee": 0.0,
                "rebate": max(0.0, commission),
            }
        ] if raw.get("aggressor") is not True and quantity > 0 else [],
        "taker_order_id": order["id"] if raw.get("aggressor") is True else "",
        "raw": raw,
    }


@dataclass(frozen=True)
class PolymarketUSMarketTrade:
    """One authoritative trade notification from the PMUS markets websocket."""

    id: str
    market: str
    aggressor_side: str
    aggressor_outcome: str
    price: float
    quantity: float
    timestamp: str

    def for_outcome(self, outcome: str) -> dict[str, Any]:
        """Express the binary-market print in one outcome token's coordinates."""
        if outcome not in {"YES", "NO"}:
            raise ReconciliationError("invalid paper trade outcome")
        same_outcome = outcome == self.aggressor_outcome
        side = self.aggressor_side if same_outcome else (
            "SELL" if self.aggressor_side == "BUY" else "BUY"
        )
        return {
            "id": f"{self.id}::{outcome}",
            "market": self.market,
            "side": side,
            "price": self.price if same_outcome else 1.0 - self.price,
            "quantity": self.quantity,
            "timestamp": self.timestamp,
        }


def normalize_market_trade(
    message: Any, *, event_sequence: int = 0
) -> PolymarketUSMarketTrade:
    """Normalize the official ``SUBSCRIPTION_TYPE_TRADE`` message schema."""
    if not isinstance(message, dict) or not isinstance(message.get("trade"), dict):
        raise ReconciliationError("malformed Polymarket US market trade message")
    raw = message["trade"]
    market = str(raw.get("marketSlug") or "")
    timestamp = str(raw.get("tradeTime") or raw.get("transactTime") or "")
    taker = raw.get("taker")
    if not market or not timestamp or not isinstance(taker, dict):
        raise ReconciliationError("Polymarket US market trade omitted identity")
    taker_side = str(taker.get("side") or "")
    intent = str(taker.get("intent") or "")
    raw_side = taker_side.removeprefix("ORDER_SIDE_")
    side = _action_from_intent(intent)
    outcome = _outcome_from_intent(intent)
    expected_raw_side = side if outcome == "YES" else (
        "SELL" if side == "BUY" else "BUY"
    )
    if raw_side not in {"BUY", "SELL"} or raw_side != expected_raw_side:
        raise ReconciliationError("inconsistent Polymarket US trade aggressor")
    price = _amount(raw.get("price"), name="market trade price")
    quantity = _amount(raw.get("quantity"), name="market trade quantity")
    if not 0 < price < 1 or quantity <= 0:
        raise ReconciliationError("invalid Polymarket US market trade economics")
    source_id = str(raw.get("id") or raw.get("tradeId") or "")
    if not source_id:
        canonical = json.dumps(raw, allow_nan=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
        source_id = f"pmus-ws-{event_sequence}-{digest}"
    return PolymarketUSMarketTrade(
        id=source_id,
        market=market,
        aggressor_side=side,
        aggressor_outcome=outcome,
        price=price if outcome == "YES" else 1.0 - price,
        quantity=quantity,
        timestamp=timestamp,
    )


class PolymarketUSTradeStream:
    """Threaded adapter for the official authenticated PMUS market trade stream."""

    _RAW_EVENT_COUNTER_LIMIT = 1_000_000

    def __init__(self, client: Any, market_slugs: list[str]) -> None:
        slugs = sorted(set(market_slugs))
        if not slugs or len(slugs) > 100:
            raise ValueError("PMUS trade subscriptions require 1-100 markets")
        self.client = client
        self.market_slugs = slugs
        self.connected = False
        self.failed = False
        self.error: str | None = None
        self.last_message_monotonic = 0.0
        self.book_subscription_connected = False
        self.trade_subscription_connected = False
        self.raw_event_count = 0
        self.raw_trade_event_count = 0
        self._trades: deque[PolymarketUSMarketTrade] = deque()
        self._sequence = 0
        self._lock = threading.Lock()
        self._stop_thread = threading.Event()
        self._stop_async: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    @classmethod
    def from_env(cls, market_slugs: list[str]) -> PolymarketUSTradeStream:
        missing = [
            name
            for name in (PMUS_KEY_ID_ENV, PMUS_SECRET_KEY_ENV)
            if not os.environ.get(name)
        ]
        if missing:
            raise SafetyStop(
                "official Polymarket US trade websocket requires authentication: "
                + ", ".join(missing)
            )
        try:
            from polymarket_us import PolymarketUS
        except ImportError as exc:
            raise SafetyStop("polymarket-us is required for the PMUS trade stream") from exc
        return cls(
            PolymarketUS(
                key_id=os.environ[PMUS_KEY_ID_ENV],
                secret_key=os.environ[PMUS_SECRET_KEY_ENV],
                timeout=8.0,
            ),
            market_slugs,
        )

    def _fail(self, exc: Exception) -> None:
        with self._lock:
            self.connected = False
            self.failed = True
            self.error = f"{type(exc).__name__}: {redact_sensitive(exc)}"
        if self._stop_async is not None:
            self._stop_async.set()

    def _on_trade(self, message: dict[str, Any]) -> None:
        try:
            with self._lock:
                self._sequence += 1
                sequence = self._sequence
                self.raw_trade_event_count = min(
                    self.raw_trade_event_count + 1,
                    self._RAW_EVENT_COUNTER_LIMIT,
                )
            trade = normalize_market_trade(message, event_sequence=sequence)
            if trade.market not in self.market_slugs:
                raise ReconciliationError("unexpected Polymarket US trade market")
            with self._lock:
                self._trades.append(trade)
                self.last_message_monotonic = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - schema drift must fail closed
            self._fail(exc)

    def _on_raw_message(self, _message: dict[str, Any]) -> None:
        """Count SDK events without retaining or logging their payloads."""
        with self._lock:
            self.raw_event_count = min(
                self.raw_event_count + 1,
                self._RAW_EVENT_COUNTER_LIMIT,
            )
            self.last_message_monotonic = time.monotonic()

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("PMUS trade stream was already started")

        def run() -> None:
            async def consume() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop_async = asyncio.Event()
                websocket = self.client.ws.markets()

                def heartbeat() -> None:
                    with self._lock:
                        self.last_message_monotonic = time.monotonic()

                def closed() -> None:
                    if not self._stop_thread.is_set():
                        self._fail(RuntimeError("Polymarket US trade websocket closed"))

                websocket.on("message", self._on_raw_message)
                websocket.on("trade", self._on_trade)
                websocket.on("heartbeat", heartbeat)
                websocket.on("error", self._fail)
                websocket.on("close", closed)
                await websocket.connect()
                await websocket.subscribe_market_data(
                    "parallax-paper-books", self.market_slugs
                )
                with self._lock:
                    self.book_subscription_connected = True
                print("PMUS_BOOK_SUBSCRIPTION=CONNECTED", flush=True)
                await websocket.subscribe_trades("parallax-paper-trades", self.market_slugs)
                with self._lock:
                    self.trade_subscription_connected = True
                    self.connected = True
                    self.last_message_monotonic = time.monotonic()
                print("PMUS_TRADE_SUBSCRIPTION=CONNECTED", flush=True)
                print(f"PMUS_TRADE_MARKETS={len(self.market_slugs)}", flush=True)
                await self._stop_async.wait()
                await websocket.close()

            try:
                asyncio.run(consume())
            except Exception as exc:  # noqa: BLE001 - cross the worker boundary safely
                self._fail(exc)

        self._thread = threading.Thread(
            target=run, name="parallax-pmus-trade-stream", daemon=True
        )
        self._thread.start()

    def wait_connected(self, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            with self._lock:
                if self.connected:
                    return True
                if self.failed:
                    return False
            time.sleep(0.01)
        return False

    def drain(self) -> list[PolymarketUSMarketTrade]:
        with self._lock:
            rows = list(self._trades)
            self._trades.clear()
            return rows

    def diagnostics(self) -> dict[str, Any]:
        with self._lock:
            return {
                "trade_stream_connected": self.connected and not self.failed,
                "trade_stream_failed": self.failed,
                "trade_stream_error": self.error,
                "last_trade_stream_message_monotonic": self.last_message_monotonic,
                "book_subscription_connected": self.book_subscription_connected,
                "trade_subscription_connected": self.trade_subscription_connected,
                "trade_markets_subscribed": len(self.market_slugs),
                "raw_event_count": self.raw_event_count,
                "raw_trade_event_count": self.raw_trade_event_count,
            }

    def stop(self) -> None:
        self._stop_thread.set()
        if self._stop_async is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_async.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        close = getattr(self.client, "close", None)
        if callable(close):
            close()


def normalize_positions(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("positions"), dict):
        raise ReconciliationError("Polymarket US position response omitted positions")
    result: list[dict[str, Any]] = []
    for slug, raw in payload["positions"].items():
        if not isinstance(raw, dict):
            raise ReconciliationError("malformed Polymarket US position")
        quantity = _number(
            raw.get("netPositionDecimal", raw.get("netPosition", 0)),
            name="net position",
        )
        if abs(quantity) <= 1e-9:
            continue
        outcome = "YES" if quantity > 0 else "NO"
        absolute = abs(quantity)
        cost = _amount(raw.get("cost"), name="position cost", default=0.0)
        result.append(
            {
                "market_id": str(slug),
                "token_id": token_id(str(slug), outcome),
                "outcome": outcome,
                "quantity_shares": absolute,
                "average_cost_usd": cost / absolute if absolute else 0.0,
                "realized_pnl_usd": _amount(
                    raw.get("realized"), name="realized P/L", default=0.0
                ),
                "raw": raw,
            }
        )
    return result


class PolymarketUSPublicClient:
    def __init__(self, *, client: Any | None = None, timeout_seconds: float = 8.0) -> None:
        if client is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:
                raise SafetyStop("polymarket-us is required for Polymarket US") from exc
            client = PolymarketUS(timeout=timeout_seconds)
        self.client = client
        self.meter = RequestMeter()

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def diagnostics(self) -> dict[str, Any]:
        return {"public_rest_requests_per_minute": self.meter.per_minute()}

    def markets_page(self, *, limit: int, offset: int) -> list[dict[str, Any]]:
        last_error: Exception | None = None
        for attempt in range(1, PMUS_DISCOVERY_MAX_ATTEMPTS + 1):
            try:
                self.meter.record()
                payload = self.client.markets.list(
                    {"active": True, "closed": False, "limit": limit, "offset": offset,
                     "orderBy": ["volume"], "orderDirection": "desc"}
                )
                return normalize_market_page(payload)
            except Exception as exc:
                last_error = exc
                if not _retryable_discovery_error(exc):
                    if isinstance(exc, (SafetyStop, ReconciliationError)):
                        raise
                    raise SafetyStop(
                        f"Polymarket US market discovery failed: {redact_sensitive(exc)}"
                    ) from exc
                if attempt == PMUS_DISCOVERY_MAX_ATTEMPTS:
                    raise PolymarketUSDiscoveryFailure(
                        f"Polymarket US market discovery failed after {attempt} attempts: {redact_sensitive(exc)}",
                        attempts=attempt, underlying_error=exc,
                    ) from exc
                time.sleep(PMUS_DISCOVERY_BACKOFF_SECONDS[attempt - 1])
        raise AssertionError(f"unreachable discovery retry state: {last_error!r}")

    def market_by_id(self, market_id: str) -> dict[str, Any]:
        """Fetch one exact public market without scanning the ranked universe."""
        try:
            self.meter.record()
            payload = self.client.markets.retrieve(int(market_id))
            rows = normalize_market_page({"markets": [payload.get("market", payload)]})
            if len(rows) != 1 or rows[0]["id"] != str(market_id):
                raise ReconciliationError("Polymarket US market ID mismatch")
            return rows[0]
        except Exception as exc:
            if isinstance(exc, (SafetyStop, ReconciliationError)):
                raise
            raise SafetyStop(
                f"Polymarket US exact market retrieval failed: {redact_sensitive(exc)}"
            ) from exc

    def book(self, slug: str) -> dict[str, Any]:
        try:
            self.meter.record()
            return normalize_book(self.client.markets.book(slug), expected_slug=slug)
        except Exception as exc:
            if isinstance(exc, (SafetyStop, ReconciliationError)):
                raise
            raise SafetyStop(
                f"Polymarket US book retrieval failed for {slug}: {redact_sensitive(exc)}"
            ) from exc

    def trades(self, slug: str) -> list[dict[str, Any]]:
        """Read public prints when the installed US SDK exposes them.

        SDK versions without a public trade endpoint intentionally return no evidence;
        paper execution must never substitute a price touch or book depletion for prints.
        """
        method = getattr(self.client.markets, "trades", None)
        if not callable(method):
            return []
        try:
            self.meter.record()
            payload = method(slug)
        except Exception:
            return []
        rows = payload.get("trades", []) if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            return []
        result = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                result.append({
                    "id": str(row.get("id") or row.get("tradeId") or ""),
                    "side": str(row.get("side") or row.get("aggressorSide") or "").upper(),
                    "price": _amount(row.get("price") or row.get("lastPx"), name="public trade price"),
                    "quantity": _number(row.get("quantity") or row.get("qty") or row.get("size"), name="public trade quantity"),
                })
            except ReconciliationError:
                continue
        return result


class PolymarketUSPrivateState:
    """Authenticated private WS snapshots are the primary live order/fill state."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.orders: dict[str, dict[str, Any]] = {}
        self.executions: list[dict[str, Any]] = []
        self.positions: list[dict[str, Any]] = []
        self.balance: dict[str, Any] = {}
        self.error: str | None = None
        self.connected = False
        self._orders_ready = False
        self._positions_ready = False
        self._balance_ready = False
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None

    def start(self) -> None:
        def run() -> None:
            async def consume() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()
                websocket = self.client.ws.private()

                def fail(exc: Exception) -> None:
                    with self._lock:
                        self.error = f"{type(exc).__name__}: {redact_sensitive(exc)}"
                    if self._stop is not None:
                        self._stop.set()

                def order_snapshot(message: dict[str, Any]) -> None:
                    try:
                        payload = message.get("orderSubscriptionSnapshot") or message.get("ordersSnapshot") or {}
                        rows = payload.get("orders", [])
                        if not isinstance(rows, list):
                            raise ReconciliationError("private order snapshot malformed")
                        normalized = {
                            row["id"]: row
                            for raw in rows
                            for row in [normalize_order(raw)]
                            if row["state"] in {"ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED"}
                        }
                        with self._lock:
                            self.orders = normalized
                            self._orders_ready = bool(payload.get("eof", False))
                    except Exception as exc:
                        fail(exc)

                def order_update(message: dict[str, Any]) -> None:
                    try:
                        payload = message.get("orderSubscriptionUpdate") or message.get("orderUpdate") or {}
                        raw = payload.get("execution", payload)
                        execution = normalize_execution(raw)
                        order = normalize_order(raw.get("order"))
                        with self._lock:
                            if order["state"] in {"ORDER_STATE_NEW", "ORDER_STATE_PARTIALLY_FILLED"}:
                                self.orders[order["id"]] = order
                            else:
                                self.orders.pop(order["id"], None)
                            self.executions.append(execution)
                            self.executions = self.executions[-500:]
                    except Exception as exc:
                        fail(exc)

                def position_snapshot(message: dict[str, Any]) -> None:
                    try:
                        payload = message.get("positionSubscriptionSnapshot") or message.get("positionsSnapshot") or {}
                        self.positions = normalize_positions({"positions": payload.get("positions", {})})
                        with self._lock:
                            self._positions_ready = bool(payload.get("eof", False))
                    except Exception as exc:
                        fail(exc)

                def balance_snapshot(message: dict[str, Any]) -> None:
                    payload = message.get("accountBalanceSubscriptionSnapshot") or message.get("accountBalancesSnapshot") or {}
                    with self._lock:
                        self.balance = dict(payload) if isinstance(payload, dict) else {}
                        self._balance_ready = True

                websocket.on("order_snapshot", order_snapshot)
                websocket.on("order_update", order_update)
                websocket.on("position_snapshot", position_snapshot)
                websocket.on("account_balance_snapshot", balance_snapshot)
                websocket.on("error", fail)
                websocket.on("close", lambda: fail(RuntimeError("private websocket closed")))
                await websocket.connect()
                with self._lock:
                    self.connected = True
                await websocket.subscribe_orders("parallax-orders")
                await websocket.subscribe_positions("parallax-positions")
                await websocket.subscribe_account_balance("parallax-balance")
                await self._stop.wait()
                await websocket.close()

            try:
                asyncio.run(consume())
            except Exception as exc:  # noqa: BLE001 - thread boundary
                with self._lock:
                    self.error = f"{type(exc).__name__}: {redact_sensitive(exc)}"

        self._thread = threading.Thread(target=run, name="parallax-pmus-private", daemon=True)
        self._thread.start()

    def ready(self) -> bool:
        with self._lock:
            return self.error is None and self.connected

    def seed(self, snapshot: dict[str, Any]) -> None:
        with self._lock:
            self.orders = {
                row["id"]: row
                for raw in snapshot.get("open_orders", [])
                for row in [
                    raw
                    if isinstance(raw, dict) and "market_id" in raw
                    else normalize_order(raw)
                ]
            }
            self.executions = list(snapshot.get("executions", []))[-500:]
            self.positions = list(snapshot.get("positions", []))
            balances = snapshot.get("balances", [])
            self.balance = dict(balances[0]) if balances else {}

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self.error is not None or not self.connected:
                raise ReconciliationError("private websocket state is not authoritative")
            return {
                "open_orders": list(self.orders.values()),
                "positions": list(self.positions),
                "executions": list(self.executions),
                "balances": [dict(self.balance)],
            }

    def stop(self) -> None:
        if self._stop is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class PolymarketUSVenue:
    REQUIRED_ENV = (PMUS_KEY_ID_ENV, PMUS_SECRET_KEY_ENV)

    def __init__(
        self,
        *,
        live_enabled: bool,
        read_only: bool = False,
        client: Any | None = None,
    ) -> None:
        from .live_engine import LIVE_ENABLED_VALUE, LIVE_ENV

        supplied_client = client is not None
        if live_enabled == read_only:
            raise ValueError("choose exactly one of live_enabled or read_only")
        if live_enabled and os.environ.get(LIVE_ENV) != LIVE_ENABLED_VALUE:
            raise SafetyStop(
                f"live mode refused: {LIVE_ENV} must equal {LIVE_ENABLED_VALUE}"
            )
        missing = [name for name in self.REQUIRED_ENV if not os.environ.get(name)]
        if client is None and missing:
            raise SafetyStop(
                f"missing Polymarket US authentication variables: {', '.join(missing)}"
            )
        if client is None:
            try:
                from polymarket_us import PolymarketUS
            except ImportError as exc:
                raise SafetyStop(
                    "polymarket-us is required for authenticated Polymarket US access"
                ) from exc
            client = PolymarketUS(
                key_id=os.environ[PMUS_KEY_ID_ENV],
                secret_key=os.environ[PMUS_SECRET_KEY_ENV],
                timeout=8.0,
            )
        self.client = client
        self.read_only = read_only
        self.rest = AuthenticatedRESTGate(
            minimum_interval_seconds=0.0 if supplied_client else 5.0
        )
        self.private_state: PolymarketUSPrivateState | None = None
        self._post_only_ids: set[str] = set()
        self._slug_by_order: dict[str, str] = {}

    def close(self) -> None:
        if self.private_state is not None:
            self.private_state.stop()
        close = getattr(self.client, "close", None)
        if callable(close):
            close()

    def authenticate(self, *, allow_closed_only: bool = False) -> dict[str, Any]:
        del allow_closed_only
        try:
            cancel_all_supported = callable(
                getattr(self.client.orders, "cancel_all", None)
            )
            if not cancel_all_supported:
                raise ReconciliationError("Polymarket US cancel-all capability is absent")
            return {
                "balances": [],
                "open_orders": [],
                "positions": [],
                "executions": [],
                "cancel_all_supported": True,
            }
        except Exception as exc:
            if isinstance(exc, SafetyStop):
                raise
            raise SafetyStop(
                f"Polymarket US authentication/reconciliation failed: {redact_sensitive(exc)}"
            ) from exc

    def start_private_state(self) -> None:
        if self.private_state is None:
            self.private_state = PolymarketUSPrivateState(self.client)
            self.private_state.start()

    def private_ready(self) -> bool:
        return self.private_state is not None and self.private_state.ready()

    def private_error(self) -> str | None:
        return self.private_state.error if self.private_state is not None else None

    def initial_snapshot(self) -> dict[str, Any]:
        snapshot = {
            "balances": self.rest.call("startup balance snapshot", self.client.account.balances).get("balances", []),
            "open_orders": self._normalize_order_list(
                self.rest.call("startup order snapshot", self.client.orders.list)
            ),
            "positions": normalize_positions(
                self.rest.call("startup position snapshot", self.client.portfolio.positions)
            ),
            "executions": self._normalize_activities(
                self.rest.call(
                    "startup execution snapshot",
                    lambda: self.client.portfolio.activities(
                        {"limit": 100, "types": ["ACTIVITY_TYPE_TRADE"]}
                    ),
                )
            ),
            "cancel_all_supported": callable(
                getattr(self.client.orders, "cancel_all", None)
            ),
        }
        if self.private_state is None:
            raise ReconciliationError("private websocket was not started")
        self.private_state.seed(snapshot)
        return snapshot

    def private_snapshot(self) -> dict[str, Any]:
        if self.private_state is None:
            raise ReconciliationError("private websocket was not started")
        return self.private_state.snapshot()

    def diagnostics(self) -> dict[str, Any]:
        return {
            **self.rest.diagnostics(),
            "private_ws_connected": self.private_ready(),
        }

    @staticmethod
    def _normalize_activities(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("activities"), list):
            raise ReconciliationError("Polymarket US activities response is malformed")
        result: list[dict[str, Any]] = []
        for row in payload["activities"]:
            if not isinstance(row, dict) or row.get("type") != "ACTIVITY_TYPE_TRADE":
                continue
            trade = row.get("trade")
            if not isinstance(trade, dict) or not trade.get("id"):
                raise ReconciliationError("malformed Polymarket US trade activity")
            result.append(
                {
                    "id": str(trade["id"]),
                    "market_id": str(trade.get("marketSlug") or ""),
                    "status": str(trade.get("state") or ""),
                    "timestamp": trade.get("updateTime") or trade.get("createTime"),
                    "price": _amount(trade.get("price"), name="trade price"),
                    "quantity": _number(trade.get("qty"), name="trade quantity"),
                    "is_aggressor": trade.get("isAggressor"),
                    "realized_pnl_usd": _amount(
                        trade.get("realizedPnl"), name="trade realized P/L", default=0.0
                    ),
                    "raw": trade,
                }
            )
        return result

    def _normalize_order_list(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("orders"), list):
            raise ReconciliationError("Polymarket US open-order response is malformed")
        result = []
        for row in payload["orders"]:
            order_id = str(row.get("id") or "") if isinstance(row, dict) else ""
            normalized = normalize_order(
                row, post_only=bool(order_id and order_id in self._post_only_ids)
            )
            self._slug_by_order[normalized["id"]] = normalized["marketSlug"]
            result.append(normalized)
        return result

    @staticmethod
    def _order_components(intent: OrderIntent) -> tuple[str, str, float, str]:
        slug, encoded_outcome = split_token_id(intent.token_id)
        outcome = intent.outcome.upper()
        if encoded_outcome != outcome or intent.market_id != slug:
            raise ReconciliationError("Polymarket US order intent identity mismatch")
        long_price = intent.price if outcome == "YES" else 1.0 - intent.price
        order_intent = {
            ("YES", "BUY"): "ORDER_INTENT_BUY_LONG",
            ("YES", "SELL"): "ORDER_INTENT_SELL_LONG",
            ("NO", "BUY"): "ORDER_INTENT_BUY_SHORT",
            ("NO", "SELL"): "ORDER_INTENT_SELL_SHORT",
        }.get((outcome, intent.side))
        if order_intent is None:
            raise ReconciliationError("invalid Polymarket US order side/outcome")
        return slug, outcome, long_price, order_intent

    @staticmethod
    def _available_usd(payload: Any) -> float:
        if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
            raise ReconciliationError("Polymarket US balance response is malformed")
        usd = [
            row
            for row in payload["balances"]
            if isinstance(row, dict)
            and str(row.get("currency") or "USD").upper() == "USD"
        ]
        if not usd:
            return 0.0
        row = usd[0]
        for field in ("buyingPower", "assetAvailable", "currentBalance"):
            if row.get(field) not in (None, ""):
                return max(0.0, _number(row[field], name=field))
        raise ReconciliationError("Polymarket US USD balance omitted available cash")

    def account_available_balance(self) -> float:
        payload = self.rest.call(
            "pre-submission balance check", self.client.account.balances
        )
        return self._available_usd(payload)

    def build_order_request(self, intent: OrderIntent) -> dict[str, Any]:
        slug, _outcome, long_price, order_intent = self._order_components(intent)
        if not float(intent.size_shares).is_integer():
            raise SafetyStop("Polymarket US quantity must use whole-share precision")
        return {
            "marketSlug": slug,
            "intent": order_intent,
            "type": "ORDER_TYPE_LIMIT",
            "price": {
                "value": f"{long_price:.10f}".rstrip("0").rstrip("."),
                "currency": "USD",
            },
            "quantity": int(intent.size_shares),
            "tif": "TIME_IN_FORCE_GOOD_TILL_CANCEL",
            "participateDontInitiate": True,
            "manualOrderIndicator": "MANUAL_ORDER_INDICATOR_AUTOMATIC",
            "synchronousExecution": False,
        }

    def prevalidate_order(self, intent: OrderIntent) -> dict[str, Any]:
        """Validate dynamic venue constraints without creating or previewing an order."""
        slug, outcome, long_price, _order_intent = self._order_components(intent)
        market_response = self.client.markets.retrieve_by_slug(slug)
        raw_market = (
            market_response.get("market") if isinstance(market_response, dict) else None
        )
        if not isinstance(raw_market, dict):
            raise ReconciliationError("Polymarket US market detail response is malformed")
        market = normalize_market_page({"markets": [raw_market]})[0]
        if (
            market["active"] is not True
            or market["closed"] is True
            or market["accepting_orders"] is not True
        ):
            raise SafetyStop("Polymarket US market is not currently tradeable")
        book = normalize_book(self.client.markets.book(slug), expected_slug=slug)
        tick = float(market["tick_size"])
        minimum = float(market["minimum_trade_quantity"])
        if not 0.01 <= long_price <= 0.99:
            raise SafetyStop("Polymarket US long-side order price is out of bounds")
        if not _on_increment(long_price, tick):
            raise SafetyStop(f"Polymarket US price is not on the venue tick ({tick:g})")
        if not float(intent.size_shares).is_integer():
            raise SafetyStop("Polymarket US quantity must use whole-share precision")
        if intent.size_shares + 1e-9 < minimum:
            raise SafetyStop(f"Polymarket US quantity is below market minimum ({minimum:g})")
        top = book[token_id(slug, outcome)]
        if intent.side == "BUY" and intent.price >= float(top["best_ask"]) - 1e-9:
            raise SafetyStop("Polymarket US post-only BUY would cross the current offer")
        if intent.side == "SELL" and intent.price <= float(top["best_bid"]) + 1e-9:
            raise SafetyStop("Polymarket US post-only SELL would cross the current bid")
        available = self.account_available_balance()
        if intent.side == "BUY" and intent.notional_usd > available + 1e-9:
            raise SafetyStop(
                "insufficient Polymarket US available balance: "
                f"required={intent.notional_usd:.4f} available={available:.4f}"
            )
        request = self.build_order_request(intent)
        return {
            "market": slug,
            "market_id": str(market["id"]),
            "outcome": outcome,
            "market_state": "MARKET_STATE_OPEN",
            "tradeable": True,
            "available_balance": available,
            "minimum_order_size": minimum,
            "tick_size": tick,
            "price_precision": max(0, -Decimal(str(tick)).normalize().as_tuple().exponent),
            "quantity_precision": 0,
            "request": request,
        }

    def place_post_only(self, intent: OrderIntent) -> dict[str, Any]:
        if self.read_only:
            raise SafetyStop("read-only Polymarket US client cannot submit orders")
        validated = self.prevalidate_order(intent)
        params = validated["request"]
        slug, outcome, _long_price, _order_intent = self._order_components(intent)
        try:
            response = self.rest.call(
                "create order", lambda: self.client.orders.create(params)
            )
        except Exception as exc:
            details = extract_sdk_error(exc)
            print(format_order_rejected(intent, details), flush=True)
            raise PolymarketUSOrderRejected(details) from exc
        order_id = str(response.get("id") or "") if isinstance(response, dict) else ""
        if not order_id:
            raise ReconciliationError("Polymarket US order response omitted order ID")
        self._post_only_ids.add(order_id)
        self._slug_by_order[order_id] = slug
        deadline = time.monotonic() + 2.0
        normalized: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            normalized = self.get_order_rest(order_id)
            if normalized["state"] not in {
                "ORDER_STATE_PENDING_NEW",
                "ORDER_STATE_PENDING_RISK",
            }:
                break
            time.sleep(0.1)
        if normalized is None:
            raise ReconciliationError("Polymarket US order state unavailable")
        if (
            normalized["market_id"] != intent.market_id
            or normalized["token_id"] != intent.token_id
            or normalized["outcome"] != outcome
            or normalized["side"] != intent.side
            or abs(float(normalized["price"]) - intent.price) > intent.tick_size / 2 + 1e-9
            or abs(float(normalized["quantity"]) - intent.size_shares) > 1e-9
        ):
            raise ReconciliationError("Polymarket US acknowledged order does not match intent")
        success = normalized["state"] == "ORDER_STATE_NEW"
        return {
            **normalized,
            "success": success,
            "status": "LIVE" if success else normalized["status"],
            "errorMsg": None if success else f"order state {normalized['state']}",
        }

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        slug = self._slug_by_order.get(order_id)
        if not slug:
            current = self.get_order_rest(order_id)
            slug = current["marketSlug"]
            self._slug_by_order[order_id] = slug
        self.rest.call(
            "cancel order",
            lambda: self.client.orders.cancel(order_id, {"marketSlug": slug}),
            cancellation_retry=True,
        )
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            state = self.get_order_rest(order_id)["state"]
            if state == "ORDER_STATE_CANCELED":
                return {"canceled": [order_id], "not_canceled": []}
            if state in {"ORDER_STATE_FILLED", "ORDER_STATE_REJECTED", "ORDER_STATE_EXPIRED"}:
                break
            time.sleep(0.1)
        raise ReconciliationError(f"uncertain cancellation state for order {order_id}")

    def cancel_all(self) -> dict[str, Any]:
        response = self.rest.call(
            "global cancel-all", self.client.orders.cancel_all, cancellation_retry=True
        )
        return {
            "canceled": response.get("canceledOrderIds", []) if isinstance(response, dict) else [],
            "not_canceled": [],
        }

    def get_open_orders(self) -> list[dict[str, Any]]:
        if self.private_ready():
            return self.private_snapshot()["open_orders"]
        return self.get_open_orders_rest()

    def get_open_orders_rest(self) -> list[dict[str, Any]]:
        return self._normalize_order_list(
            self.rest.call("open-order reconciliation", self.client.orders.list)
        )

    def get_order(self, order_id: str) -> dict[str, Any]:
        if self.private_ready():
            row = next(
                (row for row in self.private_snapshot()["open_orders"] if row["id"] == order_id),
                None,
            )
            if row is not None:
                return row
        return self.get_order_rest(order_id)

    def get_order_rest(self, order_id: str) -> dict[str, Any]:
        normalized = normalize_order(
            self.rest.call(
                "order reconciliation",
                lambda: self.client.orders.retrieve(order_id),
            ),
            post_only=order_id in self._post_only_ids,
        )
        self._slug_by_order[order_id] = normalized["marketSlug"]
        return normalized

    def get_trades(self, *, after: int | None = None) -> list[dict[str, Any]]:
        if self.private_ready():
            return self.private_snapshot()["executions"]
        return self.get_trades_rest(after=after)

    def get_trades_rest(self, *, after: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": 100,
            "types": ["ACTIVITY_TYPE_TRADE"],
            "sortOrder": "SORT_ORDER_DESCENDING",
        }
        if after is not None:
            params["cursor"] = str(after)
        return self._normalize_activities(
            self.rest.call(
                "execution reconciliation", lambda: self.client.portfolio.activities(params)
            )
        )

    def get_positions(self) -> list[dict[str, Any]]:
        if self.private_ready():
            return self.private_snapshot()["positions"]
        return normalize_positions(
            self.rest.call("position reconciliation", self.client.portfolio.positions)
        )

    def heartbeat(self, heartbeat_id: str) -> dict[str, Any]:
        del heartbeat_id
        if self.private_ready():
            return {"heartbeat_id": str(time.time_ns())}
        payload = self.rest.call("heartbeat", self.client.account.balances)
        if not isinstance(payload, dict) or not isinstance(payload.get("balances"), list):
            raise ReconciliationError("Polymarket US authentication heartbeat failed")
        return {"heartbeat_id": str(time.time_ns())}

    def confirmed_rewards(self, date: str) -> list[dict[str, Any]]:
        payload = self.rest.call(
            "confirmed incentive earnings",
            lambda: self.client.get(
                "/v1/incentives/earnings",
                query={"start_date": date, "end_date": date},
                authenticated=True,
            ),
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("rewards"), list):
            raise ReconciliationError("Polymarket US incentive earnings response is malformed")
        result = []
        for row in payload["rewards"]:
            if not isinstance(row, dict):
                raise ReconciliationError("malformed Polymarket US incentive earning")
            result.append(
                {
                    "condition_id": str(row.get("marketSlug") or ""),
                    "earnings": _number(row.get("reward", 0), name="incentive reward"),
                    "asset_rate": 1.0,
                    "program_type": str(row.get("programType") or ""),
                    "status": str(row.get("status") or ""),
                    "date": str(row.get("date") or date),
                    "raw": row,
                }
            )
        return result
