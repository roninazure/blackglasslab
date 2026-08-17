from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass


CLOB_BASE_URL = "https://clob.polymarket.com"


@dataclass(frozen=True)
class TopOfBook:
    bid: float | None
    ask: float | None
    bid_size: float | None
    ask_size: float | None


def fetch_top_of_book(
    token_id: str,
    *,
    timeout: float = 20.0,
) -> TopOfBook:
    params = urllib.parse.urlencode({"token_id": token_id})
    url = f"{CLOB_BASE_URL}/book?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "parallax-sports/0.1",
            "Accept": "application/json",
        },
    )

    with urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    ).open(req, timeout=timeout) as response:
        book = json.load(response)

    bids = [
        (float(x["price"]), float(x["size"]))
        for x in (book.get("bids") or [])
        if x.get("price") is not None and x.get("size") is not None
    ]

    asks = [
        (float(x["price"]), float(x["size"]))
        for x in (book.get("asks") or [])
        if x.get("price") is not None and x.get("size") is not None
    ]

    best_bid = max(bids, default=None, key=lambda x: x[0])
    best_ask = min(asks, default=None, key=lambda x: x[0])

    return TopOfBook(
        bid=best_bid[0] if best_bid else None,
        ask=best_ask[0] if best_ask else None,
        bid_size=best_bid[1] if best_bid else None,
        ask_size=best_ask[1] if best_ask else None,
    )
