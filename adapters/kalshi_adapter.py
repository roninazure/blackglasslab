from __future__ import annotations

from typing import Optional

from .base import MarketSnapshot


class KalshiAdapter:
    """
    Phase 1.2: stub (no network calls).
    """
    venue = "kalshi"

    def get_snapshot(self, *, market_id: str, question_hint: Optional[str] = None) -> MarketSnapshot:
        question = question_hint or f"Kalshi market {market_id}"

        # Stub: no live odds ingested yet. Neutral baseline.
        p_yes_market = 0.50

        return MarketSnapshot(
            venue=self.venue,
            market_id=market_id,
            question=str(question),
            p_yes_market=float(p_yes_market),
            extra={"stub": True, "note": "Phase 1.2 adapter stub; no live odds ingested."},
        )
