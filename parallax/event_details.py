"""One-request, read-only venue detail lookups for generic event candidates."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .event_discovery import EventCandidate


@dataclass(frozen=True)
class VenueMarketDetail:
    market: dict[str, Any]
    raw_response: dict[str, Any]
    source_reference: str


def _validated_detail(
    payload: Any, *, lookup_id: str, source_reference: str
) -> VenueMarketDetail:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("market"), Mapping):
        raise TypeError("market detail response omitted market")
    market = dict(payload["market"])
    identifiers = {
        str(market.get(key)).strip()
        for key in ("id", "ticker", "slug", "market_id", "marketSlug")
        if market.get(key) not in (None, "")
    }
    if lookup_id not in identifiers:
        raise ValueError("market detail identifier mismatch")
    return VenueMarketDetail(market, dict(payload), source_reference)


def fetch_pmus_market_detail(client: Any, candidate: EventCandidate) -> VenueMarketDetail:
    """Use the installed public SDK's GET-by-slug resource."""
    slug = candidate.slug or ""
    if not slug:
        raise ValueError("PMUS candidate omitted detail lookup slug")
    meter = getattr(client, "meter", None)
    record = getattr(meter, "record", None)
    if callable(record):
        record()
    payload = client.client.markets.retrieve_by_slug(slug)
    return _validated_detail(
        payload,
        lookup_id=slug,
        source_reference=f"https://gateway.polymarket.us/v1/market/slug/{quote(slug, safe='')}",
    )


def fetch_kalshi_market_detail(client: Any, candidate: EventCandidate) -> VenueMarketDetail:
    """Use the existing unauthenticated GET-only Kalshi transport."""
    ticker = candidate.market_id
    if not ticker:
        raise ValueError("Kalshi candidate omitted detail lookup ticker")
    payload = client.get(f"/markets/{quote(ticker, safe='')}")
    return _validated_detail(
        payload,
        lookup_id=ticker,
        source_reference=(
            "https://api.elections.kalshi.com/trade-api/v2/markets/"
            f"{quote(ticker, safe='')}"
        ),
    )


__all__ = [
    "VenueMarketDetail", "fetch_kalshi_market_detail", "fetch_pmus_market_detail",
]
