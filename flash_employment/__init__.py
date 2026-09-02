"""Bounded, paper-only Employment Situation FLASH trial."""

from .core import (
    BRACKETS,
    KnownPayoutEconomics,
    MarketBundle,
    evaluate_known_payout,
    resolve_payroll_change,
    validate_market_event,
)

__all__ = [
    "BRACKETS",
    "KnownPayoutEconomics",
    "MarketBundle",
    "evaluate_known_payout",
    "resolve_payroll_change",
    "validate_market_event",
]
