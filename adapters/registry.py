from __future__ import annotations

from .base import MarketAdapter
from .fake_adapter import FakeAdapter
from .polymarket_adapter import PolymarketAdapter
from .kalshi_adapter import KalshiAdapter


def get_adapter(source: str) -> MarketAdapter:
    src = (source or "").strip().lower()
    if src == "fake":
        return FakeAdapter()
    if src == "polymarket":
        return PolymarketAdapter()
    if src == "kalshi":
        return KalshiAdapter()
    # default: treat unknown as stub polymarket-like venue for now
    # but keep the venue label stable by returning PolymarketAdapter only for 'polymarket'
    return FakeAdapter()
