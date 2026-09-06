from __future__ import annotations

import json
from collections import Counter
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient

from .models import NormalizedMarket, utcnow
from .normalization import normalize_kalshi, normalize_pmus

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"


class KalshiPublicClient:
    """GET-only transport, adapted from the existing cross-venue research script.

    No credentials, account paths, order methods or environment loading.
    """

    def get(self, path: str, **params: str | int) -> dict:
        if not path.startswith("/markets") or ".." in path:
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
        metrics["POLYMARKET.markets_discovered"] = len(rows)
        for row in rows:
            book = {}
            try:
                book = pmus.book(row["slug"])
            except Exception as exc:  # noqa: BLE001 - isolate SDK failures by market
                failure("POLYMARKET", "book", exc)
            try:
                markets.append(normalize_pmus(row, book, utcnow().isoformat()))
                metrics["POLYMARKET.markets_observed"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                failure("POLYMARKET", "normalization", exc)
    except Exception as exc:  # noqa: BLE001 - isolate SDK discovery failures by venue
        failure("POLYMARKET", "discovery", exc)
    finally:
        if pmus:
            pmus.close()
    kalshi = KalshiPublicClient()
    try:
        payload = kalshi.markets_page(limit=limit)
        rows = payload.get("markets", [])
        metrics["KALSHI.markets_discovered"] = len(rows)
        for row in rows:
            book, trades = {}, None
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
                markets.append(normalize_kalshi(row, book, observed_at, trades))
                metrics["KALSHI.markets_observed"] += 1
            except (ValueError, TypeError, KeyError) as exc:
                failure("KALSHI", "normalization", exc)
    except (OSError, ValueError, KeyError) as exc:
        failure("KALSHI", "discovery", exc)
    return markets, {
        "metrics": dict(metrics),
        "errors": errors,
        "scope": f"First {limit} open markets per venue; bounded sample, not full catalog",
    }
