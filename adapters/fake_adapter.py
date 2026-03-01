from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from .base import MarketAdapter, MarketSnapshot


class FakeAdapter:
    """
    Offline adapter that reads markets/fake_markets.json.
    """
    venue = "fake"

    def __init__(self, markets_path: Path | None = None) -> None:
        self._path = markets_path or (Path("markets") / "fake_markets.json")
        self._cache = None

    def _load(self):
        if self._cache is None:
            if not self._path.exists():
                raise FileNotFoundError(f"Missing {self._path}. Create markets/fake_markets.json first.")
            self._cache = json.loads(self._path.read_text(encoding="utf-8"))
        return self._cache

    def get_snapshot(self, *, market_id: str, question_hint: Optional[str] = None) -> MarketSnapshot:
        markets = self._load()
        found = None
        for m in markets:
            if str(m.get("market_id", "")).strip() == market_id:
                found = m
                break

        question = (found.get("question") if found else None) or question_hint or f"Auto question for {market_id}"

        # Phase 1.2: Fake feed has no real odds; use neutral baseline 0.50.
        # Phase 1.3+ will compute market probability from adapter data.
        p_yes_market = 0.50

        return MarketSnapshot(
            venue=self.venue,
            market_id=market_id,
            question=str(question),
            p_yes_market=float(p_yes_market),
            extra={"source": "fake_markets.json", "found": bool(found)},
        )
