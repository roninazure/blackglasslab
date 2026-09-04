#!/usr/bin/env python3
"""Operator-gated PARALLAX Polymarket maker pilot. Never defaults to live."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import sys
import threading
import time
import urllib.parse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from census.stream import BookState, StreamStats, consume_market_stream
from maker_spread_economics.live import PublicHTTPError, ReadOnlyPublicClient
from maker_spread_economics.live_engine import (
    KILL_ENV,
    KILL_SWITCH_VALUE,
    LIVE_ENABLED_VALUE,
    LIVE_ENV,
    Candidate,
    ExecutionEngine,
    LiveLimits,
    LiveStore,
    PolymarketVenue,
    ProcessLock,
    RankedCandidate,
    SafetyStop,
    rank_candidate,
    require_geographic_eligibility,
)
from maker_spread_economics.polymarket_us import (
    PolymarketUSPublicClient,
    PolymarketUSVenue,
    normalize_book,
    split_token_id,
    token_id,
)


def _list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def discover_all_active_markets(
    client: ReadOnlyPublicClient, *, page_size: int = 100
) -> list[dict[str, Any]]:
    """Enumerate the active Gamma universe without category/strategy filtering."""
    result: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    pages = 0
    while True:
        query = {
            "limit": min(max(page_size, 1), 100),
            "closed": "false",
            "order": "liquidity",
            "ascending": "false",
        }
        if cursor is not None:
            query["after_cursor"] = cursor
        params = urllib.parse.urlencode(query)
        payload = client.get(
            f"https://gamma-api.polymarket.com/events/keyset?{params}",
            operation="active universe discovery",
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise SafetyStop("Gamma keyset universe response did not contain an events list")
        events = payload["events"]
        for event in events:
            if not isinstance(event, dict):
                continue
            for market in (
                event.get("markets", [])
                if isinstance(event.get("markets"), list)
                else []
            ):
                if not isinstance(market, dict):
                    continue
                row = dict(market)
                row["_event_id"] = str(event.get("id") or event.get("slug") or "")
                row["_event_title"] = str(event.get("title") or "")
                result.append(row)
        next_cursor = payload.get("next_cursor")
        if not events or not next_cursor:
            break
        next_cursor = str(next_cursor)
        if next_cursor in seen_cursors:
            raise SafetyStop("Gamma active universe returned a repeated keyset cursor")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
        pages += 1
        if pages > 1_000:
            raise SafetyStop("Gamma active universe exceeded bounded keyset pagination guard")
    return result


def discover_all_active_us_markets(
    client: PolymarketUSPublicClient, *, page_size: int = 500
) -> list[dict[str, Any]]:
    """Enumerate the active US universe using bounded offset pagination."""
    size = min(max(page_size, 1), 500)
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    offset = 0
    for _page in range(1_000):
        rows = client.markets_page(limit=size, offset=offset)
        for row in rows:
            slug = str(row["slug"])
            if slug in seen:
                raise SafetyStop("Polymarket US discovery returned a duplicate market slug")
            seen.add(slug)
            result.append(row)
        if len(rows) < size:
            return result
        offset += len(rows)
    raise SafetyStop("Polymarket US discovery exceeded bounded pagination guard")


def _pre_rank(market: dict[str, Any]) -> tuple[float, float, float]:
    return (
        _number(market.get("volume24hr") or market.get("volume24h")),
        _number(market.get("liquidityClob") or market.get("liquidity")),
        _number(market.get("volume")),
    )


def _eligible_market(market: dict[str, Any]) -> bool:
    tokens = _list(market.get("clobTokenIds"))
    outcomes = _list(market.get("outcomes"))
    return bool(
        market.get("active") is not False
        and not market.get("closed")
        and market.get("acceptingOrders") is not False
        and market.get("enableOrderBook") is not False
        and market.get("conditionId")
        and tokens
        and len(tokens) == len(outcomes)
    )


def scan_candidates(
    client: ReadOnlyPublicClient,
    markets: list[dict[str, Any]],
    *,
    limits: LiveLimits,
    required_tokens: set[str],
) -> list[RankedCandidate]:
    eligible = [market for market in markets if _eligible_market(market)]
    eligible.sort(key=_pre_rank, reverse=True)
    token_rows: list[tuple[dict[str, Any], str, str]] = []
    deferred: list[tuple[dict[str, Any], str, str]] = []
    for market in eligible:
        outcomes = _list(market.get("outcomes"))
        tokens = [str(token) for token in _list(market.get("clobTokenIds"))]
        for outcome, token in zip(outcomes, tokens, strict=True):
            row = (market, str(outcome), token)
            (token_rows if token in required_tokens else deferred).append(row)
    token_rows.extend(deferred[: max(0, limits.book_scan_limit - len(token_rows))])
    ranked: list[RankedCandidate] = []
    market_info: dict[str, dict[str, Any]] = {}
    for market, outcome, token in token_rows:
        condition_id = str(market["conditionId"])
        try:
            book = client.get(
                "https://clob.polymarket.com/book?"
                + urllib.parse.urlencode({"token_id": token}),
                operation="candidate order-book read",
            )
            bids = [
                (_number(row.get("price"), -1), _number(row.get("size")))
                for row in book.get("bids", [])
                if isinstance(row, dict)
            ]
            asks = [
                (_number(row.get("price"), 2), _number(row.get("size")))
                for row in book.get("asks", [])
                if isinstance(row, dict)
            ]
            bids = [row for row in bids if 0 < row[0] < 1 and row[1] > 0]
            asks = [row for row in asks if 0 < row[0] < 1 and row[1] > 0]
            if not bids or not asks:
                continue
            bid = max(bids)
            ask = min(asks)
            if condition_id not in market_info:
                try:
                    market_info[condition_id] = client.get(
                        "https://clob.polymarket.com/clob-markets/"
                        + urllib.parse.quote(condition_id, safe=""),
                        operation="candidate CLOB market metadata read",
                    )
                except PublicHTTPError as exc:
                    print(
                        json.dumps({"status": "PUBLIC_HTTP_ERROR", "error": str(exc)}),
                        flush=True,
                    )
                    market_info[condition_id] = {}
                except Exception:  # noqa: BLE001 - optional public metadata has no stable exception family
                    market_info[condition_id] = {}
            info = market_info[condition_id]
            tick = _number(book.get("tick_size") or info.get("mts"), 0.01)
            minimum = _number(book.get("min_order_size") or info.get("mos"), 5.0)
            candidate = Candidate(
                market_id=condition_id,
                event_id=str(market.get("_event_id") or ""),
                token_id=token,
                outcome=outcome,
                question=str(
                    market.get("question") or market.get("_event_title") or ""
                ),
                best_bid=bid[0],
                best_ask=ask[0],
                bid_size_shares=bid[1],
                ask_size_shares=ask[1],
                tick_size=tick,
                min_order_size_shares=minimum,
                volume_24h_usd=_number(
                    market.get("volume24hr") or market.get("volume24h")
                ),
                liquidity_usd=_number(
                    market.get("liquidityClob") or market.get("liquidity"), 1.0
                ),
                book_observed_monotonic=time.monotonic(),
            )
            score = rank_candidate(candidate, limits)
            if score is not None or token in required_tokens:
                ranked.append(
                    score
                    or RankedCandidate(candidate, minimum, float("-inf"), 0.0, 0.0)
                )
        except PublicHTTPError as exc:
            print(
                json.dumps({"status": "PUBLIC_HTTP_ERROR", "error": str(exc)}),
                flush=True,
            )
            if token in required_tokens:
                raise SafetyStop(
                    f"cannot obtain authoritative book for inventory token {token}"
                ) from exc
        except Exception:  # noqa: BLE001 - skip any non-inventory market with unusable public data
            if token in required_tokens:
                raise SafetyStop(
                    f"cannot obtain authoritative book for inventory token {token}"
                )
    ranked.sort(key=lambda item: item.expected_net_usd_per_hour, reverse=True)
    return ranked


def scan_us_candidates(
    client: PolymarketUSPublicClient,
    markets: list[dict[str, Any]],
    *,
    limits: LiveLimits,
    required_tokens: set[str],
) -> list[RankedCandidate]:
    eligible = [
        market
        for market in markets
        if market.get("active") is True
        and market.get("closed") is False
        and market.get("accepting_orders") is True
    ]
    eligible.sort(
        key=lambda row: (
            _number(row.get("volume")),
            _number(row.get("liquidity")),
        ),
        reverse=True,
    )
    required_slugs = {split_token_id(value)[0] for value in required_tokens}
    priority = [row for row in eligible if row["slug"] in required_slugs]
    priority.extend(row for row in eligible if row["slug"] not in required_slugs)
    scan_rows = priority[: limits.book_scan_limit]
    ranked: list[RankedCandidate] = []
    found_required: set[str] = set()
    for market in scan_rows:
        slug = str(market["slug"])
        try:
            normalized = client.book(slug)
            stats = normalized.get("stats", {})
            volume = _number(market.get("volume"))
            if volume <= 0 and isinstance(stats, dict):
                volume = _number(
                    (stats.get("notionalTraded") or {}).get("value")
                    if isinstance(stats.get("notionalTraded"), dict)
                    else 0
                )
            liquidity = max(_number(market.get("liquidity")), 1.0)
            for outcome in ("YES", "NO"):
                internal_token = token_id(slug, outcome)
                top = normalized[internal_token]
                long_bid_price = (
                    float(top["best_bid"])
                    if outcome == "YES"
                    else 1.0 - float(top["best_bid"])
                )
                long_ask_price = (
                    float(top["best_ask"])
                    if outcome == "YES"
                    else 1.0 - float(top["best_ask"])
                )
                order_prices_valid = (
                    0.01 <= long_bid_price <= 0.99
                    and 0.01 <= long_ask_price <= 0.99
                )
                candidate = Candidate(
                    market_id=slug,
                    event_id=str(market.get("event_id") or market.get("id") or slug),
                    token_id=internal_token,
                    outcome=outcome,
                    question=str(market.get("question") or slug),
                    best_bid=float(top["best_bid"]),
                    best_ask=float(top["best_ask"]),
                    bid_size_shares=float(top["bid_size_shares"]),
                    ask_size_shares=float(top["ask_size_shares"]),
                    tick_size=float(market["tick_size"]),
                    min_order_size_shares=float(market["minimum_trade_quantity"]),
                    volume_24h_usd=volume,
                    liquidity_usd=liquidity,
                    accepting_orders=order_prices_valid,
                    market_active=True,
                    book_observed_monotonic=float(top["book_observed_monotonic"]),
                )
                score = rank_candidate(candidate, limits)
                if score is not None or internal_token in required_tokens:
                    ranked.append(
                        score
                        or RankedCandidate(
                            candidate,
                            candidate.min_order_size_shares,
                            float("-inf"),
                            0.0,
                            0.0,
                        )
                    )
                if internal_token in required_tokens:
                    found_required.add(internal_token)
        except Exception as exc:
            if slug in required_slugs:
                raise SafetyStop(
                    f"cannot obtain authoritative Polymarket US book for inventory market {slug}: {exc}"
                ) from exc
    missing = required_tokens - found_required
    if missing:
        raise SafetyStop(
            f"inventory-bearing Polymarket US tokens are unavailable: {sorted(missing)}"
        )
    ranked.sort(key=lambda item: item.expected_net_usd_per_hour, reverse=True)
    return ranked


def select_markets(
    ranked: list[RankedCandidate], *, limits: LiveLimits, required_tokens: set[str]
) -> list[RankedCandidate]:
    selected: list[RankedCandidate] = []
    markets: set[str] = set()
    for item in ranked:
        required = item.candidate.token_id in required_tokens
        if item.candidate.market_id in markets and not required:
            continue
        if (
            item.candidate.market_id not in markets
            and len(markets) >= limits.max_active_markets
        ):
            if required:
                raise SafetyStop(
                    "existing inventory exceeds maximum active-market capacity"
                )
            continue
        selected.append(item)
        markets.add(item.candidate.market_id)
        if len(markets) >= limits.max_active_markets and required_tokens.issubset(
            {row.candidate.token_id for row in selected}
        ):
            break
    if not required_tokens.issubset({row.candidate.token_id for row in selected}):
        raise SafetyStop(
            "an inventory-bearing token is absent from selected market books"
        )
    return selected


class MarketStreamMonitor:
    def __init__(self, *, stale_seconds: float) -> None:
        self.stale_seconds = stale_seconds
        self.books = BookState()
        self.stats = StreamStats(started_at_utc=datetime.now(UTC).isoformat())
        self.last_message_monotonic = 0.0
        self.book_update_monotonic: dict[str, float] = {}
        self.failed = False
        self.error: str | None = None
        self._lock = threading.Lock()
        self._stop: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def start(self, assets: list[str]) -> None:
        if not assets:
            raise SafetyStop("cannot start market websocket without selected assets")

        def run() -> None:
            async def consume() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop = asyncio.Event()

                async def on_message(
                    message: dict[str, Any], _received_ns: int
                ) -> None:
                    now = time.monotonic()
                    with self._lock:
                        changed = self.books.apply(message)
                        self.last_message_monotonic = now
                        for token in changed:
                            self.book_update_monotonic[token] = now

                async def on_gap(reason: str, _received_ns: int) -> None:
                    with self._lock:
                        self.failed = True
                        self.error = reason
                    assert self._stop is not None
                    self._stop.set()

                await consume_market_stream(
                    assets,
                    on_message,
                    stop=self._stop,
                    stats=self.stats,
                    on_stream_gap=on_gap,
                    stale_seconds=self.stale_seconds,
                    first_message_timeout=self.stale_seconds,
                )

            try:
                asyncio.run(consume())
            except Exception as exc:  # noqa: BLE001 - propagate thread failures through monitor state
                with self._lock:
                    self.failed = True
                    self.error = f"{type(exc).__name__}: {exc}"

        self._thread = threading.Thread(
            target=run, name="parallax-market-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._stop is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def ready(self, assets: list[str]) -> bool:
        with self._lock:
            return not self.failed and all(
                asset in self.book_update_monotonic for asset in assets
            )

    def candidate(self, base: Candidate) -> Candidate:
        with self._lock:
            top = self.books.top(base.token_id)
            observed = self.book_update_monotonic.get(base.token_id, 0.0)
        bid = top["bid"]
        ask = top["ask"]
        if bid is None or ask is None or not 0 < bid < ask < 1:
            raise SafetyStop(f"invalid streamed book for token {base.token_id}")
        return replace(
            base,
            best_bid=float(bid),
            best_ask=float(ask),
            bid_size_shares=float(top["bid_size"]),
            ask_size_shares=float(top["ask_size"]),
            book_observed_monotonic=observed,
        )


class PolymarketUSFeedMonitor:
    """Authenticated US book websocket, with public REST polling for dry-run only."""

    def __init__(
        self,
        *,
        public: PolymarketUSPublicClient,
        stale_seconds: float,
        websocket_client: Any | None,
    ) -> None:
        self.public = public
        self.stale_seconds = stale_seconds
        self.websocket_client = websocket_client
        self.last_message_monotonic = 0.0
        self.book_update_monotonic: dict[str, float] = {}
        self.failed = False
        self.error: str | None = None
        self._books: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._stop_thread = threading.Event()
        self._stop_async: asyncio.Event | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None

    def _apply(self, payload: dict[str, Any], slug: str) -> None:
        normalized = normalize_book(payload, expected_slug=slug)
        now = time.monotonic()
        with self._lock:
            for outcome in ("YES", "NO"):
                key = token_id(slug, outcome)
                self._books[key] = normalized[key]
                self.book_update_monotonic[key] = now
            self.last_message_monotonic = now

    def start(self, assets: list[str]) -> None:
        if not assets:
            raise SafetyStop("cannot start Polymarket US feed without selected markets")
        slugs = sorted({split_token_id(asset)[0] for asset in assets})

        if self.websocket_client is None:
            def poll() -> None:
                while not self._stop_thread.is_set():
                    try:
                        for slug in slugs:
                            if self._stop_thread.is_set():
                                return
                            normalized = self.public.book(slug)
                            now = time.monotonic()
                            with self._lock:
                                for outcome in ("YES", "NO"):
                                    key = token_id(slug, outcome)
                                    self._books[key] = normalized[key]
                                    self.book_update_monotonic[key] = now
                                self.last_message_monotonic = now
                    except Exception as exc:  # noqa: BLE001 - feed failure crosses thread boundary
                        with self._lock:
                            self.failed = True
                            self.error = f"{type(exc).__name__}: {exc}"
                        return
                    self._stop_thread.wait(1.0)

            self._thread = threading.Thread(
                target=poll, name="parallax-pmus-rest-feed", daemon=True
            )
            self._thread.start()
            return

        def run_websocket() -> None:
            async def consume() -> None:
                self._loop = asyncio.get_running_loop()
                self._stop_async = asyncio.Event()
                websocket = self.websocket_client.ws.markets()

                def on_market_data(message: dict[str, Any]) -> None:
                    market_data = message.get("marketData", {})
                    slug = str(market_data.get("marketSlug") or "")
                    if slug not in slugs:
                        with self._lock:
                            self.failed = True
                            self.error = "unexpected Polymarket US websocket market"
                        self._stop_async.set()
                        return
                    try:
                        self._apply(message, slug)
                    except Exception as exc:  # noqa: BLE001 - fail closed on schema drift
                        with self._lock:
                            self.failed = True
                            self.error = f"{type(exc).__name__}: {exc}"
                        self._stop_async.set()

                def on_heartbeat() -> None:
                    with self._lock:
                        self.last_message_monotonic = time.monotonic()

                def on_error(error: Exception) -> None:
                    with self._lock:
                        self.failed = True
                        self.error = f"{type(error).__name__}: {error}"
                    self._stop_async.set()

                def on_close() -> None:
                    if not self._stop_thread.is_set():
                        on_error(RuntimeError("Polymarket US market websocket closed"))

                websocket.on("market_data", on_market_data)
                websocket.on("heartbeat", on_heartbeat)
                websocket.on("error", on_error)
                websocket.on("close", on_close)
                await websocket.connect()
                await websocket.subscribe_market_data("parallax-books", slugs)
                await self._stop_async.wait()
                await websocket.close()

            try:
                asyncio.run(consume())
            except Exception as exc:  # noqa: BLE001 - feed failure crosses thread boundary
                with self._lock:
                    self.failed = True
                    self.error = f"{type(exc).__name__}: {exc}"

        self._thread = threading.Thread(
            target=run_websocket, name="parallax-pmus-market-stream", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_thread.set()
        if self._stop_async is not None and self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_async.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def ready(self, assets: list[str]) -> bool:
        with self._lock:
            return not self.failed and all(
                asset in self.book_update_monotonic for asset in assets
            )

    def candidate(self, base: Candidate) -> Candidate:
        with self._lock:
            top = self._books.get(base.token_id)
            observed = self.book_update_monotonic.get(base.token_id, 0.0)
        if top is None:
            raise SafetyStop(
                f"missing streamed Polymarket US book for {base.token_id}"
            )
        bid = float(top["best_bid"])
        ask = float(top["best_ask"])
        if not 0 < bid < ask < 1:
            raise SafetyStop(
                f"invalid streamed Polymarket US book for {base.token_id}"
            )
        return replace(
            base,
            best_bid=bid,
            best_ask=ask,
            bid_size_shares=float(top["bid_size_shares"]),
            ask_size_shares=float(top["ask_size_shares"]),
            book_observed_monotonic=observed,
        )


def _print_summary(summary: dict[str, Any]) -> None:
    print("\nPARALLAX LIVE REVENUE", flush=True)
    for label, value in summary.items():
        if isinstance(value, float):
            print(
                f"{label}: ${value:,.6f}"
                if label != "capital turnover"
                else f"{label}: {value:.6f}x",
                flush=True,
            )
        else:
            print(f"{label}: {value}", flush=True)


def _sync_confirmed_income(
    store: LiveStore,
    venue: PolymarketVenue | PolymarketUSVenue,
    public: ReadOnlyPublicClient | PolymarketUSPublicClient,
    *,
    date: str,
) -> None:
    order_markets = store.attributable_markets(date, require_fill=False)
    fill_markets = store.attributable_markets(date, require_fill=True)
    for row in venue.confirmed_rewards(date):
        if not isinstance(row, dict):
            continue
        market = str(row.get("condition_id") or "")
        amount = _number(row.get("earnings")) * _number(row.get("asset_rate"), 1.0)
        reward_status = str(row.get("status") or "").upper()
        confirmed = not isinstance(venue, PolymarketUSVenue) or reward_status in {
            "PAID",
            "REWARD_STATUS_PAID",
            "CONFIRMED",
            "SETTLED",
            "COMPLETED",
        }
        if market in order_markets and amount >= 0 and confirmed:
            store.record_confirmed_income(
                income_key=(
                    f"liquidity:{date}:{market}:"
                    f"{row.get('asset_address') or row.get('program_type')}"
                ),
                date_utc=date,
                market_id=market,
                kind="LIQUIDITY_REWARD",
                amount_usd=amount,
                source="polymarket_rewards_user_confirmed_prior_day",
                raw=row,
            )
    if not isinstance(venue, PolymarketVenue):
        return
    params = urllib.parse.urlencode({"date": date, "maker_address": venue.address})
    payload = public.get(
        f"https://clob.polymarket.com/rebates/current?{params}",
        operation="confirmed maker rebate read",
    )
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, dict):
            continue
        market = str(row.get("condition_id") or "")
        amount = _number(row.get("rebated_fees_usdc"))
        if market in fill_markets and amount >= 0:
            store.record_confirmed_income(
                income_key=f"rebate:{date}:{market}:{row.get('asset_address')}",
                date_utc=date,
                market_id=market,
                kind="MAKER_REBATE",
                amount_usd=amount,
                source="polymarket_rebates_current_confirmed_prior_day",
                raw=row,
            )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="PARALLAX bounded Polymarket live maker pilot"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="public reads and simulated resting orders only",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="allow operator-gated authenticated maker writes",
    )
    mode.add_argument(
        "--auth-read-only",
        action="store_true",
        help="authenticate and reconcile account/order/execution state without writes",
    )
    parser.add_argument(
        "--venue",
        required=True,
        choices=("polymarket-us", "polymarket-international"),
    )
    parser.add_argument(
        "--db", required=True, help="persistent SQLite execution ledger"
    )
    parser.add_argument("--lock-file", default="data/parallax_live_maker.lock")
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=0.0,
        help="zero runs until interrupted",
    )
    parser.add_argument(
        "--cancel-all",
        action="store_true",
        help="live-gated venue cancel-all, then exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.cancel_all and not args.live:
        raise SystemExit("--cancel-all requires --live")
    if args.duration_seconds < 0:
        raise SystemExit("--duration-seconds cannot be negative")
    limits = LiveLimits.from_env()
    mode = "LIVE" if args.live else "DRY_RUN"
    if args.auth_read_only and args.venue != "polymarket-us":
        raise SystemExit("--auth-read-only is supported only for --venue polymarket-us")
    if args.live and args.venue != "polymarket-us":
        raise SystemExit(
            "REFUSING LIVE MODE: US operators must use --venue polymarket-us; "
            "international remains read-only"
        )
    if args.live and os.environ.get(LIVE_ENV) != LIVE_ENABLED_VALUE:
        raise SystemExit(
            f"REFUSING LIVE MODE: {LIVE_ENV} must equal {LIVE_ENABLED_VALUE}"
        )
    if (
        args.live
        and os.environ.get(KILL_ENV) == KILL_SWITCH_VALUE
        and not args.cancel_all
    ):
        raise SystemExit(f"REFUSING LIVE MODE: {KILL_ENV} is active")

    with ProcessLock(Path(args.lock_file).resolve()):
        public: ReadOnlyPublicClient | PolymarketUSPublicClient
        public = (
            PolymarketUSPublicClient(timeout_seconds=8.0)
            if args.venue == "polymarket-us"
            else ReadOnlyPublicClient(timeout_seconds=8.0)
        )
        if args.live and args.venue == "polymarket-international" and not args.cancel_all:
            require_geographic_eligibility(
                public.get(
                    "https://polymarket.com/api/geoblock",
                    operation="geographic eligibility read",
                )
            )
        venue: PolymarketVenue | PolymarketUSVenue | None
        if args.auth_read_only:
            venue = PolymarketUSVenue(live_enabled=False, read_only=True)
        elif args.live and args.venue == "polymarket-us":
            venue = PolymarketUSVenue(live_enabled=True)
        elif args.live:
            venue = PolymarketVenue(live_enabled=True)
        else:
            venue = None
        auth_snapshot: dict[str, Any] | None = None
        if venue is not None:
            auth_snapshot = venue.authenticate(allow_closed_only=args.cancel_all)
            if args.auth_read_only:
                assert isinstance(auth_snapshot, dict)
                print(
                    json.dumps(
                        {
                            "status": "AUTHENTICATED_READ_ONLY_CHECK_PASS",
                            "open_orders": len(auth_snapshot["open_orders"]),
                            "positions": len(auth_snapshot["positions"]),
                            "executions": len(auth_snapshot["executions"]),
                            "balances": len(auth_snapshot["balances"]),
                            "cancel_all_capability": auth_snapshot[
                                "cancel_all_supported"
                            ],
                            "live_orders_placed": 0,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                venue.close()
                if isinstance(public, PolymarketUSPublicClient):
                    public.close()
                return 0
            if args.cancel_all:
                response = venue.cancel_all()
                remaining = venue.get_open_orders()
                if remaining:
                    raise SafetyStop(
                        f"global cancel-all verification failed; {len(remaining)} orders remain"
                    )
                print(
                    json.dumps(
                        {"status": "CANCEL_ALL_SENT", "response": response},
                        sort_keys=True,
                    )
                )
                if isinstance(venue, PolymarketUSVenue):
                    venue.close()
                if isinstance(public, PolymarketUSPublicClient):
                    public.close()
                return 0
        store = LiveStore(
            Path(args.db).resolve(),
            mode=mode,
            capital_allocated_usd=limits.total_bankroll_usd,
        )
        engine = ExecutionEngine(store=store, venue=venue, limits=limits, mode=mode)
        monitor: MarketStreamMonitor | PolymarketUSFeedMonitor | None = None
        started = time.monotonic()
        stop_requested = False
        stop_reason = "operator interrupt"

        def request_stop(_signum: int, _frame: Any) -> None:
            nonlocal stop_requested
            stop_requested = True

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        try:
            if venue is not None:
                # Do not take ownership of manual/other-bot orders. A deliberate --cancel-all is required.
                existing = venue.get_open_orders()
                known = {
                    str(row["venue_order_id"])
                    for row in store.open_orders()
                    if row["venue_order_id"]
                }
                unknown = {
                    str(row.get("id") or row.get("orderID") or row.get("order_id"))
                    for row in existing
                    if isinstance(row, dict)
                } - known
                if unknown:
                    raise SafetyStop(
                        "pre-existing venue orders detected; run the explicit live --cancel-all command first"
                    )
                if isinstance(venue, PolymarketUSVenue):
                    remote_positions = {
                        str(row["token_id"]): float(row["quantity_shares"])
                        for row in (auth_snapshot or {}).get("positions", [])
                    }
                    local_positions = {
                        str(row["token_id"]): float(row["quantity_shares"])
                        for row in store.all_inventory()
                    }
                    if remote_positions.keys() != local_positions.keys() or any(
                        abs(remote_positions[key] - local_positions[key]) > 1e-9
                        for key in remote_positions.keys() & local_positions.keys()
                    ):
                        raise SafetyStop(
                            "Polymarket US account positions do not reconcile to the local ledger"
                        )
                engine.reconcile()
                yesterday = (datetime.now(UTC).date() - timedelta(days=1)).isoformat()
                _sync_confirmed_income(store, venue, public, date=yesterday)

            markets = (
                discover_all_active_us_markets(public)
                if isinstance(public, PolymarketUSPublicClient)
                else discover_all_active_markets(public)
            )
            print(
                json.dumps(
                    {"status": "DISCOVERY_COMPLETE", "markets_scanned": len(markets)},
                    sort_keys=True,
                ),
                flush=True,
            )
            required = {str(row["token_id"]) for row in store.all_inventory()}
            ranked = (
                scan_us_candidates(
                    public, markets, limits=limits, required_tokens=required
                )
                if isinstance(public, PolymarketUSPublicClient)
                else scan_candidates(
                    public, markets, limits=limits, required_tokens=required
                )
            )
            print(
                json.dumps(
                    {"status": "RANKING_COMPLETE", "candidates_ranked": len(ranked)},
                    sort_keys=True,
                ),
                flush=True,
            )
            selected = select_markets(
                ranked,
                limits=limits,
                required_tokens=required,
            )
            print(
                json.dumps(
                    {
                        "status": "SELECTION_COMPLETE",
                        "selected_markets": [
                            {
                                "market_id": item.candidate.market_id,
                                "name": item.candidate.question,
                                "outcome": item.candidate.outcome,
                            }
                            for item in selected
                        ],
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if not selected:
                raise SafetyStop(
                    "no candidate has positive bounded expected net dollars per hour"
                )
            if mode == "DRY_RUN":
                # Seed the simulation from the authoritative REST snapshot so a bounded
                # run always exercises quote construction before the streaming loop.
                for scored in selected:
                    intent = engine.size_intent(scored, side="BUY")
                    if intent is None:
                        continue
                    print(
                        json.dumps(
                            {
                                "status": "PROPOSED_MAKER_QUOTE",
                                "market_id": intent.market_id,
                                "token_id": intent.token_id,
                                "side": intent.side,
                                "price": intent.price,
                                "size": intent.size_shares,
                                "notional": intent.notional_usd,
                                "post_only": True,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                    engine.place(intent)
            assets = [item.candidate.token_id for item in selected]
            monitor = (
                PolymarketUSFeedMonitor(
                    public=public,
                    stale_seconds=limits.stale_websocket_seconds,
                    websocket_client=(
                        venue.client if isinstance(venue, PolymarketUSVenue) else None
                    ),
                )
                if isinstance(public, PolymarketUSPublicClient)
                else MarketStreamMonitor(stale_seconds=limits.stale_websocket_seconds)
            )
            monitor.start(assets)
            ready_deadline = time.monotonic() + limits.stale_websocket_seconds
            while not monitor.ready(assets):
                if monitor.failed or time.monotonic() >= ready_deadline:
                    raise SafetyStop(
                        f"market websocket startup failed: {monitor.error or 'timeout'}"
                    )
                time.sleep(0.05)

            next_reconcile = next_heartbeat = time.monotonic()
            next_discovery = time.monotonic() + limits.discovery_seconds
            while not stop_requested:
                now = time.monotonic()
                if args.duration_seconds and now - started >= args.duration_seconds:
                    stop_reason = "bounded duration complete"
                    break
                if monitor.failed:
                    raise SafetyStop(f"market websocket failed: {monitor.error}")
                current: dict[str, Candidate] = {}
                current_ranked: list[RankedCandidate] = []
                for prior in selected:
                    candidate = monitor.candidate(prior.candidate)
                    current[candidate.token_id] = candidate
                    scored = rank_candidate(candidate, limits)
                    if scored is not None:
                        current_ranked.append(scored)
                    elif store.inventory(candidate.token_id):
                        current_ranked.append(
                            RankedCandidate(
                                candidate,
                                candidate.min_order_size_shares,
                                -1.0,
                                0.0,
                                0.0,
                            )
                        )
                midpoint = {
                    token: candidate.midpoint for token, candidate in current.items()
                }
                engine.check_risk(
                    midpoint_by_token=midpoint,
                    websocket_last_message_monotonic=monitor.last_message_monotonic,
                    websocket_failed=monitor.failed,
                    now_monotonic=now,
                )
                if now >= next_reconcile:
                    fills = engine.reconcile()
                    if fills:
                        store.event("FILLS_RECONCILED", {"count": fills})
                    next_reconcile = now + limits.reconciliation_seconds
                if now >= next_heartbeat:
                    engine.send_heartbeat()
                    next_heartbeat = now + limits.heartbeat_seconds
                engine.cancel_stale_or_moved(current, now_monotonic=now)

                open_keys = {
                    (row["token_id"], row["side"]) for row in store.open_orders()
                }
                for scored in current_ranked:
                    for side in ("SELL", "BUY"):
                        key = (scored.candidate.token_id, side)
                        if key in open_keys:
                            continue
                        intent = engine.size_intent(scored, side=side)
                        if intent is not None:
                            if mode == "DRY_RUN":
                                print(
                                    json.dumps(
                                        {
                                            "status": "PROPOSED_MAKER_QUOTE",
                                            "market_id": intent.market_id,
                                            "token_id": intent.token_id,
                                            "side": intent.side,
                                            "price": intent.price,
                                            "size": intent.size_shares,
                                            "notional": intent.notional_usd,
                                            "post_only": True,
                                        },
                                        sort_keys=True,
                                    ),
                                    flush=True,
                                )
                            engine.place(intent)
                            open_keys.add(key)

                if now >= next_discovery:
                    refreshed_markets = (
                        discover_all_active_us_markets(public)
                        if isinstance(public, PolymarketUSPublicClient)
                        else discover_all_active_markets(public)
                    )
                    required = {str(row["token_id"]) for row in store.all_inventory()}
                    refreshed = select_markets(
                        (
                            scan_us_candidates(
                                public,
                                refreshed_markets,
                                limits=limits,
                                required_tokens=required,
                            )
                            if isinstance(public, PolymarketUSPublicClient)
                            else scan_candidates(
                                public,
                                refreshed_markets,
                                limits=limits,
                                required_tokens=required,
                            )
                        ),
                        limits=limits,
                        required_tokens=required,
                    )
                    refreshed_assets = [item.candidate.token_id for item in refreshed]
                    if set(refreshed_assets) != set(assets):
                        for row in list(store.open_orders()):
                            if row["venue_order_id"]:
                                engine.cancel(
                                    str(row["venue_order_id"]),
                                    "universe capital reallocation",
                                )
                            else:
                                store.conn.execute(
                                    "UPDATE live_orders SET status='CANCELLED',cancel_reason=? WHERE local_order_id=?",
                                    (
                                        "universe capital reallocation",
                                        row["local_order_id"],
                                    ),
                                )
                        store.conn.commit()
                        monitor.stop()
                        monitor = (
                            PolymarketUSFeedMonitor(
                                public=public,
                                stale_seconds=limits.stale_websocket_seconds,
                                websocket_client=(
                                    venue.client
                                    if isinstance(venue, PolymarketUSVenue)
                                    else None
                                ),
                            )
                            if isinstance(public, PolymarketUSPublicClient)
                            else MarketStreamMonitor(
                                stale_seconds=limits.stale_websocket_seconds
                            )
                        )
                        monitor.start(refreshed_assets)
                        refresh_ready_deadline = (
                            time.monotonic() + limits.stale_websocket_seconds
                        )
                        while not monitor.ready(refreshed_assets):
                            if (
                                monitor.failed
                                or time.monotonic() >= refresh_ready_deadline
                            ):
                                raise SafetyStop(
                                    f"refreshed market websocket startup failed: {monitor.error or 'timeout'}"
                                )
                            time.sleep(0.05)
                        selected, assets = refreshed, refreshed_assets
                    else:
                        selected = refreshed
                    if venue is not None:
                        yesterday = (
                            datetime.now(UTC).date() - timedelta(days=1)
                        ).isoformat()
                        _sync_confirmed_income(store, venue, public, date=yesterday)
                    next_discovery = now + limits.discovery_seconds

                summary = store.summary(
                    midpoint_by_token=midpoint,
                    elapsed_hours=(now - started) / 3600.0,
                    capital=limits.total_bankroll_usd,
                )
                print(
                    json.dumps({"status": "RUNNING", **summary}, sort_keys=True),
                    flush=True,
                )
                time.sleep(1.0)
        except SafetyStop as exc:
            stop_reason = str(exc)
            try:
                engine.emergency_stop(stop_reason)
            except SafetyStop as cancel_exc:
                stop_reason = f"{stop_reason}; {cancel_exc}"
            return_code = 2
        except Exception as exc:  # noqa: BLE001 - process boundary must cancel on every unknown failure
            stop_reason = f"unexpected critical state: {type(exc).__name__}: {exc}"
            try:
                engine.emergency_stop(stop_reason)
            except SafetyStop as cancel_exc:
                stop_reason = f"{stop_reason}; {cancel_exc}"
            return_code = 2
        else:
            engine.emergency_stop(stop_reason)
            return_code = 0
        finally:
            if monitor is not None:
                monitor.stop()
            midpoint = {}
            if monitor is not None:
                for item in selected if "selected" in locals() else []:
                    try:
                        candidate = monitor.candidate(item.candidate)
                        midpoint[candidate.token_id] = candidate.midpoint
                    except SafetyStop:
                        midpoint[item.candidate.token_id] = item.candidate.midpoint
            for inventory in store.all_inventory():
                midpoint.setdefault(
                    str(inventory["token_id"]), float(inventory["average_cost_usd"])
                )
            summary = store.summary(
                midpoint_by_token=midpoint,
                elapsed_hours=(time.monotonic() - started) / 3600.0,
                capital=limits.total_bankroll_usd,
            )
            _print_summary(summary)
            print(f"stop reason: {stop_reason}", flush=True)
            store.close(stop_reason)
            if isinstance(venue, PolymarketUSVenue):
                venue.close()
            if isinstance(public, PolymarketUSPublicClient):
                public.close()
        return return_code


if __name__ == "__main__":
    raise SystemExit(main())
