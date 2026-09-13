from __future__ import annotations

from datetime import datetime, UTC
from typing import Protocol

from .mlb import MLBEvidenceProvider
from .nfl import NFLEvidenceProvider
from .cfb import CFBEvidenceProvider, fetch_games
from .models import Evidence, NormalizedMarket


class EvidenceProvider(Protocol):
    def supports(self, market: NormalizedMarket) -> bool: ...
    def assess(self, market: NormalizedMarket) -> Evidence | None: ...


class EvidenceEngine:
    """Small fail-closed provider router; qualification remains authoritative."""

    def __init__(self, providers: tuple[EvidenceProvider, ...] | None = None):
        # CFB live routing is opt-in at the provider loader boundary so the
        # historical CFBD calls happen once per scan, not once per market.
        self.providers = providers or (MLBEvidenceProvider(), NFLEvidenceProvider(), CFBEvidenceProvider(lambda: fetch_games(tuple(range(2010, datetime.now(UTC).year + 1)))))

    def assess(self, market: NormalizedMarket) -> Evidence | None:
        for provider in self.providers:
            try:
                if provider.supports(market):
                    return provider.assess(market)
            except Exception:
                return None
        return None
