from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from .devig import devig_two_way


@dataclass(frozen=True)
class BookMoneyline:
    bookmaker: str
    outcome_a_decimal: float
    outcome_b_decimal: float
    age_seconds: float = 0.0


@dataclass(frozen=True)
class ConsensusFairValue:
    outcome_a_probability: float
    outcome_b_probability: float
    bookmakers_used: tuple[str, ...]
    sample_size: int


def consensus_two_way_moneyline(
    quotes: list[BookMoneyline],
    *,
    max_age_seconds: float = 120.0,
    min_books: int = 2,
) -> ConsensusFairValue | None:
    fair = []

    for quote in quotes:
        if quote.age_seconds < 0 or quote.age_seconds > max_age_seconds:
            continue

        try:
            pa, pb = devig_two_way(
                quote.outcome_a_decimal,
                quote.outcome_b_decimal,
            )
        except (TypeError, ValueError):
            continue

        fair.append((quote.bookmaker, pa, pb))

    if len(fair) < min_books:
        return None

    pa_values = [row[1] for row in fair]
    pa = float(median(pa_values))
    pb = 1.0 - pa

    return ConsensusFairValue(
        outcome_a_probability=pa,
        outcome_b_probability=pb,
        bookmakers_used=tuple(row[0] for row in fair),
        sample_size=len(fair),
    )
