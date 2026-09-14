"""Bounded public-data reads for the paper-only negative-risk smoke test."""

from __future__ import annotations

import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from .model import BookLevel, LegBook, fee_metadata_from_venue


ALLOWED_HOSTS = {"gamma-api.polymarket.com", "clob.polymarket.com"}


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
            raise TimeoutError("read-only smoke-test deadline exhausted")
        request = urllib.request.Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "swarm-edge-negrisk-paper/1.0"},
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
    return str(tokens[0]) if len(tokens) >= 2 else None


def standard_complete_event(event: dict[str, Any]) -> bool:
    markets = event.get("markets") if isinstance(event.get("markets"), list) else []
    ids = [str(market.get("conditionId") or "") for market in markets if isinstance(market, dict)]
    return bool(
        event.get("negRisk")
        and not event.get("negRiskAugmented")
        and len(markets) >= 3
        and len(ids) == len(markets)
        and len(set(ids)) == len(ids)
        and all(_yes_token(market) for market in markets)
    )


def discover_events(client: ReadOnlyPublicClient, *, limit: int = 100) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"limit": min(max(limit, 1), 100), "active": "true", "closed": "false", "order": "liquidity", "ascending": "false"}
    )
    payload = client.get(f"https://gamma-api.polymarket.com/events?{params}")
    if not isinstance(payload, list):
        raise TypeError("Gamma events response was not a list")
    return sorted(
        (event for event in payload if isinstance(event, dict) and standard_complete_event(event)),
        key=lambda event: len(event.get("markets", [])),
    )


def _book_timestamp(book: dict[str, Any]) -> str | None:
    try:
        value = float(book.get("timestamp"))
        if value > 10_000_000_000:
            value /= 1000.0
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, OSError):
        return None


def fetch_basket_books(
    client: ReadOnlyPublicClient,
    event: dict[str, Any],
    *,
    fee_info: dict[str, tuple[dict[str, Any], dict[str, Any] | None]] | None = None,
) -> tuple[list[LegBook], dict[str, tuple[dict[str, Any], dict[str, Any] | None]]]:
    books: list[LegBook] = []
    cached_fees = dict(fee_info or {})
    for market in event.get("markets", []):
        condition_id = str(market.get("conditionId") or "")
        token_id = _yes_token(market)
        if not condition_id or token_id is None:
            raise ValueError("event contains an incomplete market leg")
        book = client.get(f"https://clob.polymarket.com/book?{urllib.parse.urlencode({'token_id': token_id})}")
        cached = cached_fees.get(condition_id)
        if cached is None:
            token_fee: dict[str, Any] | None = None
            try:
                info = client.get(f"https://clob.polymarket.com/clob-markets/{urllib.parse.quote(condition_id, safe='')}")
            except Exception:
                info = {}
                try:
                    token_fee = client.get(f"https://clob.polymarket.com/fee-rate/{urllib.parse.quote(token_id, safe='')}")
                except Exception:
                    token_fee = None
            cached_fees[condition_id] = (info, token_fee)
        else:
            info, token_fee = cached
        asks = tuple(
            BookLevel(float(level["price"]), float(level["size"]))
            for level in book.get("asks", [])
            if isinstance(level, dict) and level.get("price") is not None and level.get("size") is not None
        )
        books.append(
            LegBook(
                market_id=condition_id,
                token_id=token_id,
                observed_at_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                asks=asks,
                fee=fee_metadata_from_venue(market, clob_market_info=info, token_fee=token_fee),
                source_book_timestamp_utc=_book_timestamp(book),
            )
        )
    return books, cached_fees
