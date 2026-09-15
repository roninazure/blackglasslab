"""Reusable economic evidence seam with conservative fail-closed behavior."""

from __future__ import annotations

from enum import StrEnum

from .models import Evidence, NormalizedMarket


class EconomicSubtype(StrEnum):
    FED_RATES = "FED_RATES"
    CPI_INFLATION = "CPI_INFLATION"
    EMPLOYMENT = "EMPLOYMENT"
    GDP = "GDP"
    RECESSION = "RECESSION"


_TERMS = (
    (EconomicSubtype.FED_RATES, ("federal reserve", "fomc", "fed funds", "interest rate", "rate hike", "rate cut", "monetary policy")),
    (EconomicSubtype.CPI_INFLATION, ("cpi", "inflation", "consumer price", "pce")),
    (EconomicSubtype.EMPLOYMENT, ("employment", "unemployment", "payroll", "jobs", "jobless")),
    (EconomicSubtype.GDP, ("gdp", "economic growth", "economic output")),
    (EconomicSubtype.RECESSION, ("recession",)),
)


def economic_subtype(market: NormalizedMarket) -> EconomicSubtype | None:
    if market.category != "MACRO_MONETARY":
        return None
    text = " ".join(str(value or "").casefold() for value in (market.title, market.description, market.resolution_rules))
    for subtype, terms in _TERMS:
        if any(term in text for term in terms):
            return subtype
    return None


class EconomicsEvidenceProvider:
    """Provider registration point; no source means no probability."""

    source_capability = "No independent structured economic data source is connected"

    def __init__(self, sources=None) -> None:
        self.sources = sources
        self.last_reason = "no_economic_source"
        self.last_observation = None

    def supports(self, market: NormalizedMarket) -> bool:
        return economic_subtype(market) is not None

    def assess(self, market: NormalizedMarket) -> Evidence | None:
        subtype = economic_subtype(market)
        if subtype is None:
            return None
        if self.sources is None:
            from .economic_sources import EconomicSources
            self.sources = EconomicSources()
        try:
            self.last_observation = self.sources.latest(subtype)
        except RuntimeError as exc:
            self.last_observation = None
            self.last_reason = f"missing_independent_economic_source:{subtype.value}:{exc}"
            return None
        self.last_reason = f"economic_probability_methodology_unvalidated:{subtype.value}"
        return None


__all__ = ["EconomicSubtype", "EconomicsEvidenceProvider", "economic_subtype"]
