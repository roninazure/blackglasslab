from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Dict, Any


@dataclass(frozen=True)
class MarketSnapshot:
    """
    Normalized market snapshot (venue-agnostic).
    Phase 1.2: p_yes_market is stubbed for real venues; fake uses 0.50 baseline.
    """
    venue: str
    market_id: str
    question: str
    p_yes_market: float  # market-implied probability of YES (0..1)
    extra: Dict[str, Any]


class MarketAdapter(Protocol):
    venue: str

    def get_snapshot(self, *, market_id: str, question_hint: Optional[str] = None) -> MarketSnapshot:
        """
        Return a normalized snapshot for market_id.
        In Phase 1.2, Polymarket/Kalshi adapters are stubs (no network).
        """
        ...
