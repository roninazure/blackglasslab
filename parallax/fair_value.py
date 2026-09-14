"""Audit obtainable value evidence without turning prices into probabilities.

Exact duplicate terms may supply a price reference, not a validated forecast.
No semantic/title matching and no self-referential YES/NO probability estimates.
"""

from datetime import datetime

from .models import NormalizedMarket, timestamp


def assess_value(
    market: NormalizedMarket, universe: list[NormalizedMarket], now: datetime
) -> dict:
    def identity(m):
        return (
            m.resolution_rules.strip(),
            m.outcomes,
            m.resolution_time,
            m.mechanics.payout,
        )

    references = []
    for other in universe:
        if (
            other.venue == market.venue
            or other.demo
            or market.demo
            or not market.resolution_rules.strip()
            or identity(other) != identity(market)
        ):
            continue
        times = [timestamp(other.book_timestamp), timestamp(other.data_timestamp)]
        bid, ask = other.yes_bid, other.yes_ask
        if (
            other.status != "OPEN"
            or not all(t and 0 <= (now - t).total_seconds() <= 60 for t in times)
            or bid is None
            or ask is None
            or not 0 < bid < ask < 1
            or not other.executable_depth.get("YES")
            or not other.executable_depth.get("NO")
        ):
            continue
        references.append(
            {
                "venue": other.venue,
                "market_id": other.venue_market_id,
                "bid": bid,
                "ask": ask,
                "observed_at": other.book_timestamp,
                "method": "exact terms/outcomes/payout/time comparison",
            }
        )
    return {
        "status": "UNVALIDATED_REFERENCE" if references else "FAIR_VALUE_UNAVAILABLE",
        "fair_value": None,
        "method": "cross-venue equivalent-contract evidence audit",
        "references": references,
        "reason": (
            "Equivalent quote found; independent probability validation is absent."
            if references
            else "No fresh, exactly equivalent cross-venue contract in bounded sample; no validated external probability feed connected."
        ),
        "rejected_method": "Same-book YES/NO complements and midpoint are not independent value evidence.",
        "assessed_at": now.isoformat(),
    }
