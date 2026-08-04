from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .service import OFFICIAL_TAKER_FEE_RATES


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


def yes_token_id(market: dict[str, Any]) -> str:
    outcomes = [str(value).strip().lower() for value in _list(market.get("outcomes"))]
    tokens = _list(market.get("clobTokenIds"))
    if "yes" not in outcomes or len(tokens) != len(outcomes):
        raise ValueError("market does not expose an unambiguous YES CLOB token")
    return str(tokens[outcomes.index("yes")])


def _timestamp(value: Any) -> str:
    raw = str(value or "").strip()
    try:
        number = float(raw)
    except ValueError:
        return raw or datetime.now(timezone.utc).isoformat()
    if number > 10_000_000_000:
        number /= 1000.0
    return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()


def _top(levels: Any, *, best: str) -> tuple[float, float]:
    parsed = [
        (float(row["price"]), float(row["size"]))
        for row in (levels if isinstance(levels, list) else [])
        if isinstance(row, dict) and row.get("price") is not None and row.get("size") is not None
    ]
    if not parsed:
        raise ValueError(f"order book has no {best} levels")
    return (max(parsed) if best == "bid" else min(parsed))


def quote_from_market_and_book(
    market: dict[str, Any],
    book: dict[str, Any],
    *,
    category: str,
) -> dict[str, Any]:
    bid, bid_size = _top(book.get("bids"), best="bid")
    ask, ask_size = _top(book.get("asks"), best="ask")
    fees_enabled = market.get("feesEnabled")
    raw_rate = market.get("feeRate", market.get("takerFeeRate"))
    if fees_enabled is False:
        fee_rate, fee_source = 0.0, "venue_market_fee_flag"
    elif raw_rate is not None:
        fee_rate, fee_source = float(raw_rate), "venue_market_fee_rate"
    elif fees_enabled is True:
        fee_rate = OFFICIAL_TAKER_FEE_RATES.get(category, 0.05)
        fee_source = "official_category_schedule_assumption"
    else:
        fee_rate, fee_source = None, "configured_fee_assumption"
    return {
        "best_bid": bid,
        "best_ask": ask,
        "bid_depth_usd": bid * bid_size,
        "ask_depth_usd": ask * ask_size,
        "depth_source": "venue_clob_top_level",
        "fee_rate": fee_rate,
        "fee_source": fee_source,
        "quote_timestamp_utc": _timestamp(book.get("timestamp")),
        "quote_source": "polymarket_clob",
        "assumptions": {
            "fee_rate_assumed": fee_source.endswith("assumption"),
            "slippage_bps_assumed": True,
        },
    }


def resolved_outcome(market: dict[str, Any]) -> str | None:
    if not bool(market.get("closed")):
        return None
    outcomes = _list(market.get("outcomes"))
    prices = _list(market.get("outcomePrices"))
    if len(outcomes) != len(prices):
        return None
    winners = [str(outcomes[i]).upper() for i, value in enumerate(prices) if float(value) >= 0.999]
    return winners[0] if len(winners) == 1 and winners[0] in {"YES", "NO"} else None
