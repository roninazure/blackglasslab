from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


COINBASE_EXCHANGE_BASE_URL = "https://api.exchange.coinbase.com"

PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
}


@dataclass(frozen=True)
class CryptoReferenceQuote:
    asset: str
    product_id: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    mid: float
    exchange_time_utc: str
    source: str = "coinbase_exchange"


def _parse_utc(value: Any) -> str:
    if not value:
        raise ValueError("exchange timestamp is required")

    parsed = datetime.fromisoformat(
        str(value).replace("Z", "+00:00")
    )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return (
        parsed.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} is not numeric") from exc

    if parsed <= 0:
        raise ValueError(f"{field} must be positive")

    return parsed


def fetch_coinbase_reference_quote(
    asset: str,
    *,
    timeout: float = 10.0,
) -> CryptoReferenceQuote:
    normalized_asset = str(asset).strip().upper()

    try:
        product_id = PRODUCTS[normalized_asset]
    except KeyError as exc:
        raise ValueError(
            f"unsupported crypto reference asset: {asset!r}"
        ) from exc

    params = urllib.parse.urlencode({"level": 1})
    url = (
        f"{COINBASE_EXCHANGE_BASE_URL}/products/"
        f"{product_id}/book?{params}"
    )

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "swarm-edge-crypto-reference/0.1",
            "Accept": "application/json",
        },
    )

    with urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    ).open(req, timeout=timeout) as response:
        payload = json.load(response)

    if not isinstance(payload, dict):
        raise RuntimeError("Coinbase product book response is not an object")

    bids = payload.get("bids")
    asks = payload.get("asks")

    if not isinstance(bids, list) or not bids:
        raise RuntimeError("Coinbase product book has no bids")

    if not isinstance(asks, list) or not asks:
        raise RuntimeError("Coinbase product book has no asks")

    bid_row = bids[0]
    ask_row = asks[0]

    if not isinstance(bid_row, list) or len(bid_row) < 2:
        raise RuntimeError("Coinbase best bid row is malformed")

    if not isinstance(ask_row, list) or len(ask_row) < 2:
        raise RuntimeError("Coinbase best ask row is malformed")

    bid = _positive_float(bid_row[0], "bid")
    bid_size = _positive_float(bid_row[1], "bid_size")
    ask = _positive_float(ask_row[0], "ask")
    ask_size = _positive_float(ask_row[1], "ask_size")

    if ask <= bid:
        raise RuntimeError(
            f"invalid Coinbase top of book: bid={bid} ask={ask}"
        )

    exchange_time = _parse_utc(payload.get("time"))

    return CryptoReferenceQuote(
        asset=normalized_asset,
        product_id=product_id,
        bid=bid,
        ask=ask,
        bid_size=bid_size,
        ask_size=ask_size,
        mid=(bid + ask) / 2.0,
        exchange_time_utc=exchange_time,
    )
