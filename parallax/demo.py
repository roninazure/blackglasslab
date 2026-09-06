"""Explicit synthetic fixtures. Never mixed into the live reader or track record."""

from dataclasses import replace
from datetime import timedelta

from .models import Evidence, Mechanics, Venue, utcnow
from .normalization import normalize_kalshi, normalize_pmus, rules_digest


def demo_inputs(now=None):
    now = now or utcnow()
    observed, end = now.isoformat(), (now + timedelta(days=2)).isoformat()
    raw = {
        "description": "SYNTHETIC DEMO: YES pays $1 if the sample event happens by the stated date; otherwise NO pays $1.",
        "category": "Demo",
        "endDate": end,
        "orderPriceMinTickSize": 0.01,
        "minimumTradeQty": 1,
        "marketSides": [
            {"long": True, "description": "Sample event happens"},
            {"long": False, "description": "Sample event does not happen"},
        ],
    }
    market = {
        "raw": raw,
        "id": "demo-pmus",
        "slug": "demo-pmus",
        "question": "SYNTHETIC DEMO · Will the sample event happen?",
        "event_id": "demo-event",
        "active": True,
        "closed": False,
        "accepting_orders": True,
    }
    book = {
        "demo-pmus::YES": {
            "best_bid": 0.30,
            "best_ask": 0.32,
            "bid_size_shares": 1000,
            "ask_size_shares": 1000,
        },
        "demo-pmus::NO": {
            "best_bid": 0.68,
            "best_ask": 0.70,
            "bid_size_shares": 1000,
            "ask_size_shares": 1000,
        },
        "transact_time": observed,
    }
    pmus = normalize_pmus(market, book, observed)
    kalshi = normalize_kalshi(
        {
            "ticker": "DEMO-KALSHI",
            "event_ticker": "DEMO",
            "title": "SYNTHETIC DEMO · Will the second sample event happen?",
            "status": "active",
            "market_type": "binary",
            "notional_value_dollars": "1.00",
            "rules_primary": raw["description"],
            "expected_expiration_time": end,
            "price_level_structure": "linear_cent",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "1000"]],
                "no_dollars": [["0.58", "1000"]],
            }
        },
        observed,
        [],
    )
    markets, evidence = [], {}
    for original in (pmus, kalshi):
        mechanics = Mechanics(
            1,
            1,
            ((0, 1, 0.01),),
            1,
            0.05,
            "HALF_EVEN",
            "SYNTHETIC DEMO fee schedule",
            end,
        )
        item = replace(original, mechanics=mechanics, demo=True)
        markets.append(item)
        proof = Evidence(
            item.venue,
            item.venue_market_id,
            0.46 if item.venue == Venue.POLYMARKET else 0.53,
            "SYNTHETIC DEMO model",
            "demo-v1",
            observed,
            end,
            rules_digest(item),
            "SYNTHETIC DEMO rule review",
            "Synthetic evidence illustrates how PARALLAX explains a pricing difference.",
            ("DEMO source A", "DEMO source B"),
            "SYNTHETIC DEMO validation" if item.venue == Venue.POLYMARKET else "",
            demo=True,
        )
        evidence[(item.venue, item.venue_market_id)] = proof
    return markets, evidence
