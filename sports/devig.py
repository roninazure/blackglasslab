from __future__ import annotations


def decimal_to_implied_probability(decimal_odds: float) -> float:
    odds = float(decimal_odds)
    if odds <= 1.0:
        raise ValueError("decimal odds must be > 1.0")
    return 1.0 / odds


def devig_two_way(
    outcome_a_decimal: float,
    outcome_b_decimal: float,
) -> tuple[float, float]:
    """Remove proportional bookmaker overround from a two-way market."""
    pa = decimal_to_implied_probability(outcome_a_decimal)
    pb = decimal_to_implied_probability(outcome_b_decimal)

    total = pa + pb
    if total <= 0:
        raise ValueError("invalid implied probability total")

    return pa / total, pb / total


def american_to_decimal(american_odds: float) -> float:
    """Convert American odds to decimal odds."""
    odds = float(american_odds)

    if odds == 0:
        raise ValueError("American odds cannot be zero")

    if odds > 0:
        return 1.0 + (odds / 100.0)

    return 1.0 + (100.0 / abs(odds))
