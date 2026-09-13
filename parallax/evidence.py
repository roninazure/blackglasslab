from __future__ import annotations

from typing import Protocol

from .mlb import MLBEvidenceProvider
from .nfl import NFLEvidenceProvider
from .models import Evidence, NormalizedMarket


class EvidenceProvider(Protocol):
    def supports(self, market: NormalizedMarket) -> bool: ...
    def assess(self, market: NormalizedMarket) -> Evidence | None: ...


class EvidenceEngine:
    """Small fail-closed provider router; qualification remains authoritative."""

    def __init__(self, providers: tuple[EvidenceProvider, ...] | None = None):
        self.providers = providers or (MLBEvidenceProvider(), NFLEvidenceProvider())

    def assess(self, market: NormalizedMarket) -> Evidence | None:
        for provider in self.providers:
            try:
                if provider.supports(market):
                    return provider.assess(market)
            except Exception:
                return None
        return None
