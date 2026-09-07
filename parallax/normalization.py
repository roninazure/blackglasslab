from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .models import Mechanics, NormalizedMarket, Venue


def text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


def first_text(*values: Any) -> str | None:
    for value in values:
        result = text(value)
        if result:
            return result
    return None


def event_title_from_metadata(*metadata: Any) -> str | None:
    for item in metadata:
        if isinstance(item, dict):
            result = first_text(
                item.get("event_title"),
                item.get("eventTitle"),
                item.get("event_name"),
                item.get("eventName"),
                item.get("name"),
                item.get("title"),
            )
            if result:
                return result
            event = item.get("event")
            if isinstance(event, dict):
                result = first_text(
                    event.get("title"),
                    event.get("name"),
                    event.get("event_title"),
                    event.get("eventTitle"),
                )
                if result:
                    return result
            events = item.get("events")
            if isinstance(events, list):
                for event_row in events:
                    result = event_title_from_metadata(event_row)
                    if result:
                        return result
    return None


def source_url_from_metadata(*metadata: Any) -> str | None:
    for item in metadata:
        if isinstance(item, dict):
            result = first_text(
                item.get("source_url"),
                item.get("sourceUrl"),
                item.get("market_url"),
                item.get("marketUrl"),
                item.get("url"),
            )
            if result:
                return result
    return None


def number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError):
        return None


def rules_digest(market: NormalizedMarket) -> str:
    binding = [
        market.venue,
        market.venue_market_id,
        market.resolution_rules,
        market.outcomes,
        market.resolution_time,
        market.mechanics.payout,
    ]
    return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()


def normalize_pmus(market: dict, book: dict, observed_at: str) -> NormalizedMarket:
    """Consume existing public client shapes without touching trading normalization."""
    raw = market["raw"]
    slug = market["slug"]
    yes, no = book.get(f"{slug}::YES", {}), book.get(f"{slug}::NO", {})

    def price(value):
        result = number(value)
        return round(result, 8) if result is not None else None

    bid, ask = price(yes.get("best_bid")), price(yes.get("best_ask"))
    bid_size, ask_size = (
        number(yes.get("bid_size_shares")) or 0,
        number(yes.get("ask_size_shares")) or 0,
    )
    stats = book.get("stats", {})
    outcomes = {
        "YES" if side.get("long") is True else "NO": str(side.get("description") or "")
        for side in raw.get("marketSides", [])
        if isinstance(side, dict)
    }
    tick = number(raw.get("orderPriceMinTickSize"))
    title = market["question"]
    selection = str(raw.get("title") or "").strip()
    if selection and selection != title:
        title = f"{title} — {selection}"
    event_title = event_title_from_metadata(market, raw)
    return NormalizedMarket(
        Venue.POLYMARKET,
        market["id"],
        slug,
        title,
        str(raw.get("description") or ""),
        str(raw.get("category") or "Uncategorized"),
        market["event_id"],
        outcomes,
        str(raw.get("description") or ""),
        raw.get("endDate"),
        "OPEN"
        if market["active"] and not market["closed"] and market["accepting_orders"]
        else "CLOSED",
        bid,
        ask,
        price(no.get("best_bid")),
        price(no.get("best_ask")),
        bid_size,
        ask_size,
        {
            "YES": ((ask, ask_size),) if ask is not None else (),
            "NO": ((price(no["best_ask"]), float(no["ask_size_shares"])),)
            if no
            else (),
        },
        number(raw.get("volume24hr")),
        None,
        stats.get("lastTradeSetTime"),
        book.get("transact_time"),
        observed_at,
        source_url_from_metadata(market, raw),
        Mechanics(
            # Whole-contract scenarios are conservative; do not assume fractional support.
            quantity_step=1,
            minimum_quantity=number(raw.get("minimumTradeQty")),
            price_ranges=((0, 1, tick),) if tick else (),
            payout=1,
        ),
        {
            "market": raw,
            "book": book,
            "venue_reference": f"PMUS:{slug}",
            "depth_scope": "top_of_book",
            "volume_window": "24h",
        },
        event_title=event_title,
    )


def normalize_kalshi(
    raw: dict,
    book: dict,
    observed_at: str,
    trades: list | None = None,
    event: dict | None = None,
) -> NormalizedMarket:
    """Kalshi exposes YES/NO bids. Opposite bids imply asks at 1 - bid."""
    fp = book.get("orderbook_fp")
    legacy = book.get("orderbook", {})

    def bids(side: str) -> list[tuple[float, float]]:
        rows = (
            fp.get(f"{side}_dollars", [])
            if isinstance(fp, dict)
            else legacy.get(side, [])
        )
        result = []
        for price, quantity in rows or []:
            p, q = number(price), number(quantity)
            if p is None or q is None:
                raise ValueError("Malformed Kalshi book")
            p = p if isinstance(fp, dict) else p / 100
            if not 0 < p < 1 or q <= 0:
                raise ValueError("Invalid Kalshi book level")
            result.append((p, q))
        return sorted(result, reverse=True)

    yes, no = bids("yes"), bids("no")
    ya = tuple((round(1 - p, 8), q) for p, q in no)
    na = tuple((round(1 - p, 8), q) for p, q in yes)
    ranges = tuple(
        (float(r["start"]), float(r["end"]), float(r["step"]))
        for r in raw.get("price_ranges", [])
    )
    if not ranges and raw.get("price_level_structure") == "linear_cent":
        ranges = ((0, 1, 0.01),)
    ticker = str(raw["ticker"])
    event_title = event_title_from_metadata(event, raw)
    rules = "\n".join(
        str(raw.get(k) or "") for k in ("rules_primary", "rules_secondary")
    ).strip()
    return NormalizedMarket(
        Venue.KALSHI,
        ticker,
        ticker,
        str(raw.get("title") or ticker),
        str(raw.get("subtitle") or ""),
        str(raw.get("category") or "Uncategorized"),
        str(raw.get("event_ticker") or ""),
        {
            "YES": str(raw.get("yes_sub_title") or raw.get("title") or "YES"),
            "NO": str(raw.get("no_sub_title") or "NO"),
        },
        rules,
        raw.get("expected_expiration_time") or raw.get("expiration_time"),
        "OPEN"
        if raw.get("status") in ("active", "open")
        else str(raw.get("status") or "UNKNOWN").upper(),
        yes[0][0] if yes else None,
        ya[0][0] if ya else None,
        no[0][0] if no else None,
        na[0][0] if na else None,
        yes[0][1] if yes else 0,
        ya[0][1] if ya else 0,
        {"YES": ya, "NO": na},
        number(raw.get("volume_24h_fp", raw.get("volume_24h"))),
        len(trades) if trades is not None else None,
        max((str(t.get("created_time") or "") for t in trades or []), default="")
        or None,
        observed_at if book else None,
        observed_at,
        source_url_from_metadata(raw, event),
        Mechanics(
            quantity_step=1,
            minimum_quantity=1,
            price_ranges=ranges,
            payout=number(raw.get("notional_value_dollars"))
            if raw.get("market_type") == "binary"
            else None,
        ),
        {
            "market": raw,
            "book": book,
            "trades": trades,
            "event": event,
            "venue_reference": ticker,
            "volume_window": "24h",
            "trade_count_scope": "returned sample (up to 100)",
        },
        timestamp_basis="local REST receipt; venue book timestamp unavailable",
        event_title=event_title,
    )
