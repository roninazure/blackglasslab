from __future__ import annotations

from typing import Optional

from .base import MarketSnapshot


class PolymarketAdapter:
    """
    Phase 1.2: stub (no network calls).
    Purpose: establish a stable interface so live_runner can compute edge vs market.
    Phase 1.3+: implement real market snapshot (prices -> p_yes_market) safely.
    """
    venue = "polymarket"

    def get_snapshot(self, *, market_id: str, question_hint: Optional[str] = None) -> MarketSnapshot:
        question = question_hint or f"Polymarket market {market_id}"

        # Stub: we do not ingest live odds yet. Neutral baseline.
        p_yes_market = 0.50

        return MarketSnapshot(
            venue=self.venue,
            market_id=market_id,
            question=str(question),
            p_yes_market=float(p_yes_market),
            extra={"stub": True, "note": "Phase 1.2 adapter stub; no live odds ingested."},
        )
