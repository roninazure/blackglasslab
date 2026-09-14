from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from census.collector import _classify

from .fill import PublicTrade
from .model import MakerFeeMetadata, QuoteSnapshot, fee_metadata_from_venue


ALLOWED_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com", "data-api.polymarket.com"}


@dataclass(frozen=True)
class BookObservation:
    quote: QuoteSnapshot
    bid_levels: tuple[tuple[float, float], ...]
    ask_levels: tuple[tuple[float, float], ...]

    def depth_at_price(self, *, side: str, price: float) -> float:
        levels = self.bid_levels if side == "BID" else self.ask_levels
        return sum(size for level_price, size in levels if abs(level_price - price) <= 1e-12)


class ReadOnlyPublicClient:
    def __init__(self, *, timeout_seconds: float = 5.0, deadline_monotonic: float | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.deadline_monotonic = deadline_monotonic

    def get(self, url: str) -> Any:
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
            raise ValueError(f"URL is outside the public read-only allowlist: {url}")
        timeout = self.timeout_seconds
        if self.deadline_monotonic is not None:
            timeout = min(timeout, self.deadline_monotonic - time.monotonic())
        if timeout <= 0:
            raise TimeoutError("read-only maker trial deadline exhausted")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "swarm-edge-maker-paper/1.0"},
        )
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=timeout) as response:
            return json.loads(response.read())


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


def _yes_token(market: dict[str, Any]) -> str | None:
    outcomes = [str(item).strip().lower() for item in _list(market.get("outcomes"))]
    tokens = _list(market.get("clobTokenIds"))
    if "yes" in outcomes and len(outcomes) == len(tokens):
        return str(tokens[outcomes.index("yes")])
    return None


def discover_markets(
    client: ReadOnlyPublicClient, *, event_limit: int = 100, market_limit: int = 20
) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {
            "limit": min(max(event_limit, 1), 100),
            "active": "true",
            "closed": "false",
            "order": "liquidity",
            "ascending": "false",
        }
    )
    payload = client.get(f"https://gamma-api.polymarket.com/events?{params}")
    if not isinstance(payload, list):
        raise TypeError("Gamma events response was not a list")
    candidates = []
    for event in payload:
        if not isinstance(event, dict):
            continue
        for market in event.get("markets", []) if isinstance(event.get("markets"), list) else []:
            if not isinstance(market, dict) or not _yes_token(market):
                continue
            engine, _category, _horizon, _reason = _classify(market, event)
            if engine != "maker_spread_rebate" or market.get("closed") or market.get("active") is False:
                continue
            candidates.append(
                {
                    "event_id": str(event.get("id") or event.get("slug") or ""),
                    "market": market,
                }
            )
            if len(candidates) >= max(1, market_limit):
                return candidates
    return candidates


def _book_timestamp(book: dict[str, Any]) -> str | None:
    try:
        value = float(book.get("timestamp"))
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def _top(levels: Any, *, side: str) -> tuple[float, float]:
    parsed = _levels(levels)
    if not parsed:
        raise ValueError(f"order book has no {side} depth")
    return max(parsed) if side == "bid" else min(parsed)


def _levels(levels: Any) -> list[tuple[float, float]]:
    return [
        (float(row["price"]), float(row["size"]))
        for row in (levels if isinstance(levels, list) else []) if isinstance(row, dict)
        if row.get("price") is not None and row.get("size") is not None
    ]


def fetch_book_observation(
    client: ReadOnlyPublicClient,
    candidate: dict[str, Any],
    *,
    fee_cache: dict[str, MakerFeeMetadata] | None = None,
) -> tuple[BookObservation, dict[str, MakerFeeMetadata]]:
    market = candidate["market"]
    condition_id = str(market.get("conditionId") or "")
    token_id = _yes_token(market)
    if not condition_id or token_id is None:
        raise ValueError("market does not expose a complete YES token mapping")
    book = client.get(f"https://clob.polymarket.com/book?{urllib.parse.urlencode({'token_id': token_id})}")
    bid, bid_size = _top(book.get("bids", []), side="bid")
    ask, ask_size = _top(book.get("asks", []), side="ask")
    cached = dict(fee_cache or {})
    if condition_id not in cached:
        try:
            info = client.get(
                f"https://clob.polymarket.com/clob-markets/{urllib.parse.quote(condition_id, safe='')}"
            )
        except Exception:
            info = {}
        cached[condition_id] = fee_metadata_from_venue(market, clob_market_info=info)
    quote = QuoteSnapshot(
            market_id=condition_id,
            token_id=token_id,
            observed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            best_bid=bid,
            best_ask=ask,
            bid_size_shares=bid_size,
            ask_size_shares=ask_size,
            source_book_timestamp_utc=_book_timestamp(book),
    )
    return BookObservation(quote, tuple(_levels(book.get("bids"))), tuple(_levels(book.get("asks")))), cached


def fetch_quote(
    client: ReadOnlyPublicClient,
    candidate: dict[str, Any],
    *,
    fee_cache: dict[str, MakerFeeMetadata] | None = None,
) -> tuple[QuoteSnapshot, dict[str, MakerFeeMetadata]]:
    observation, cached = fetch_book_observation(client, candidate, fee_cache=fee_cache)
    return observation.quote, cached


def _trade_timestamp(value: Any) -> str | None:
    try:
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def fetch_public_trades(
    client: ReadOnlyPublicClient,
    *,
    market_id: str,
    token_id: str,
    after_utc: str,
    limit: int = 1000,
) -> tuple[PublicTrade, ...]:
    after = datetime.fromisoformat(after_utc.replace("Z", "+00:00")).timestamp()
    params = urllib.parse.urlencode(
        {"market": market_id, "limit": min(max(limit, 1), 1000), "takerOnly": "true"}
    )
    payload = client.get(f"https://data-api.polymarket.com/trades?{params}")
    if not isinstance(payload, list):
        raise TypeError("Data API trades response was not a list")
    result = []
    seen = set()
    for row in payload:
        if not isinstance(row, dict) or str(row.get("asset") or "") != token_id:
            continue
        timestamp_utc = _trade_timestamp(row.get("timestamp"))
        if timestamp_utc is None:
            continue
        timestamp = datetime.fromisoformat(timestamp_utc.replace("Z", "+00:00")).timestamp()
        if timestamp + 1e-9 < after:
            continue
        key = (
            str(row.get("transactionHash") or ""),
            str(row.get("asset") or ""),
            str(row.get("side") or ""),
            str(row.get("price") or ""),
            str(row.get("size") or ""),
            timestamp_utc,
        )
        if key in seen:
            continue
        seen.add(key)
        try:
            result.append(
                PublicTrade(
                    asset_id=token_id,
                    side=str(row.get("side") or "").upper(),
                    price=float(row["price"]),
                    size_shares=float(row["size"]),
                    timestamp_utc=timestamp_utc,
                    transaction_hash=str(row.get("transactionHash") or "") or None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return tuple(result)
