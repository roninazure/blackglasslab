from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient

from .fair_value import assess_value
from .fees import attach_fees
from .models import NormalizedMarket, utcnow
from .normalization import normalize_kalshi, normalize_pmus

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiPublicClient:
    """GET-only transport, adapted from the existing cross-venue research script.

    No credentials, account paths, order methods or environment loading.
    """

    def get(self, path: str, **params: str | int) -> dict:
        parts = path.split("/")
        if (
            ".." in path
            or len(parts) not in (2, 3, 4)
            or parts[1] not in {"markets", "events", "series"}
            or (len(parts) == 4 and (parts[1] != "markets" or parts[3] != "orderbook"))
        ):
            raise ValueError("Only public market paths are supported")
        request = Request(
            f"{KALSHI_BASE}{path}?{urlencode(params)}",
            headers={"User-Agent": "PARALLAX-read-only/1"},
            method="GET",
        )
        with urlopen(request, timeout=8) as response:
            return json.load(response)

    def markets_page(self, *, limit: int, cursor: str = "") -> dict:
        return self.get(
            "/markets", status="open", limit=limit, cursor=cursor, mve_filter="exclude"
        )

    def book(self, ticker: str) -> dict:
        return self.get(f"/markets/{quote(ticker, safe='')}/orderbook", depth=20)

    def trades(self, ticker: str) -> list:
        return self.get("/markets/trades", ticker=ticker, limit=100).get("trades", [])

    def event(self, ticker: str) -> dict:
        return self.get(f"/events/{quote(ticker, safe='')}").get("event", {})

    def series(self, ticker: str) -> dict:
        return self.get(f"/series/{quote(ticker, safe='')}").get("series", {})


def collect_markets(limit: int = 12) -> tuple[list[NormalizedMarket], dict]:
    if not 1 <= limit <= 100:
        raise ValueError("Scan limit must be between 1 and 100 per venue")
    markets: list[NormalizedMarket] = []
    metrics: Counter = Counter()
    errors: list[dict] = []

    def failure(venue: str, stage: str, exc: Exception) -> None:
        metrics[f"{venue}.failures"] += 1
        # Do not serialize arbitrary exception messages, headers or account details.
        errors.append(
            {"venue": venue, "stage": stage, "error_type": type(exc).__name__}
        )

    pmus = None
    try:
        pmus = PolymarketUSPublicClient()
        rows = pmus.markets_page(limit=limit, offset=0)
        discovery_at = utcnow().isoformat()
        metrics["POLYMARKET.markets_discovered"] = len(rows)
        for row in rows:
            book = {}
            try:
                book = pmus.book(row["slug"])
            except Exception as exc:  # noqa: BLE001 - isolate SDK failures by market
                failure("POLYMARKET", "book", exc)
            try:
                markets.append(
                    attach_fees(normalize_pmus(row, book, discovery_at), utcnow())
                )
                metrics["POLYMARKET.markets_observed"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                failure("POLYMARKET", "normalization", exc)
    except Exception as exc:  # noqa: BLE001 - isolate SDK discovery failures by venue
        failure("POLYMARKET", "discovery", exc)
    finally:
        if pmus:
            pmus.close()
    kalshi = KalshiPublicClient()
    events, series = {}, {}
    try:
        payload = kalshi.markets_page(limit=100)
        rows = payload.get("markets", [])
        discovery_at = utcnow().isoformat()
        metrics["KALSHI.markets_discovered"] = len(rows)
        rows = sorted(
            rows,
            key=lambda r: float(r.get("volume_24h_fp", r.get("volume_24h", 0)) or 0),
            reverse=True,
        )[:limit]
        for row in rows:
            book, trades = {}, None
            event, fee_series = None, None
            try:
                event_id = row["event_ticker"]
                if event_id not in events:
                    events[event_id] = kalshi.event(event_id)
                event = events[event_id]
                series_id = event["series_ticker"]
                if series_id not in series:
                    series[series_id] = kalshi.series(series_id)
                fee_series = series[series_id]
            except (OSError, ValueError, KeyError) as exc:
                failure("KALSHI", "fees", exc)
            observed_at = utcnow().isoformat()
            try:
                book = kalshi.book(row["ticker"])
                observed_at = utcnow().isoformat()
            except (OSError, ValueError, KeyError) as exc:
                failure("KALSHI", "book", exc)
            try:
                trades = kalshi.trades(row["ticker"])
            except (OSError, ValueError, KeyError) as exc:
                failure("KALSHI", "activity", exc)
            try:
                market = normalize_kalshi(row, book, observed_at, trades, event)
                market = replace(market, data_timestamp=discovery_at)
                markets.append(
                    attach_fees(market, utcnow(), event=event, series=fee_series)
                )
                metrics["KALSHI.markets_observed"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                failure("KALSHI", "normalization", exc)
    except (OSError, ValueError, KeyError) as exc:
        failure("KALSHI", "discovery", exc)
    now = utcnow()
    markets = [
        replace(
            m,
            original_metadata={
                **m.original_metadata,
                "fair_value_provenance": assess_value(m, markets, now),
            },
        )
        for m in markets
    ]
    return markets, {
        "metrics": dict(metrics),
        "errors": errors,
        "scope": f"{limit} Polymarket US markets by volume; {limit} Kalshi markets by 24h volume from at most 100 newest open markets; not full catalog",
    }
