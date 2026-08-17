from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Iterable


EVIDENCE_STATES = {
    "NO_FILL_EVIDENCE",
    "POSSIBLE_FILL",
    "PROBABLE_FILL",
    "TRADED_THROUGH",
    "UNKNOWN",
}
STRONG_STATES = {"PROBABLE_FILL", "TRADED_THROUGH"}


@dataclass(frozen=True)
class HypotheticalMakerQuote:
    market_id: str
    token_id: str
    side: str
    price: float
    size_shares: float
    signaled_at_utc: str
    eligible_from_utc: str
    displayed_depth_ahead_shares: float
    displayed_depth_at_quote_shares: float

    def __post_init__(self) -> None:
        if self.side not in {"BID", "ASK"}:
            raise ValueError("hypothetical maker quote side must be BID or ASK")
        if not self.market_id or not self.token_id:
            raise ValueError("market_id and token_id are required")
        if not 0 < self.price < 1 or self.size_shares <= 0:
            raise ValueError("hypothetical quote price and size must be positive")
        if self.displayed_depth_ahead_shares < 0 or self.displayed_depth_at_quote_shares < 0:
            raise ValueError("observable queue/depth cannot be negative")


@dataclass(frozen=True)
class PublicTrade:
    asset_id: str
    side: str
    price: float
    size_shares: float
    timestamp_utc: str
    transaction_hash: str | None = None


@dataclass(frozen=True)
class SideFillEvidence:
    side: str
    state: str
    trades_at_quote_shares: float
    trades_through_quote_shares: float
    displayed_depth_ahead_shares: float
    final_displayed_depth_at_quote_shares: float | None
    displayed_depth_depletion_shares: float | None
    quote_persisted: bool | None
    midpoint_touched_quote: bool | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class MakerFillFollowup:
    bid: SideFillEvidence
    ask: SideFillEvidence
    two_sided_completion_state: str
    inventory_risk_state: str
    hypothetical_inventory_shares: float
    observed_trade_count: int
    followup_window_seconds: float
    evidence_source: str
    evidence_error: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def hypothetical_quotes(
    *,
    market_id: str,
    token_id: str,
    bid: float,
    ask: float,
    size_shares: float,
    signaled_at_utc: str,
    eligible_from_utc: str,
    bid_depth_shares: float,
    ask_depth_shares: float,
) -> tuple[HypotheticalMakerQuote, HypotheticalMakerQuote]:
    return (
        HypotheticalMakerQuote(
            market_id,
            token_id,
            "BID",
            bid,
            size_shares,
            signaled_at_utc,
            eligible_from_utc,
            bid_depth_shares,
            bid_depth_shares,
        ),
        HypotheticalMakerQuote(
            market_id,
            token_id,
            "ASK",
            ask,
            size_shares,
            signaled_at_utc,
            eligible_from_utc,
            ask_depth_shares,
            ask_depth_shares,
        ),
    )


def infer_side_fill_evidence(
    quote: HypotheticalMakerQuote,
    *,
    trades: Iterable[PublicTrade],
    final_depth_at_quote_shares: float | None,
    final_midpoint: float | None,
    followup_available: bool,
) -> SideFillEvidence:
    if not followup_available:
        return SideFillEvidence(
            quote.side,
            "UNKNOWN",
            0.0,
            0.0,
            quote.displayed_depth_ahead_shares,
            None,
            None,
            None,
            None,
            ("bounded public follow-up data unavailable",),
        )

    relevant_side = "SELL" if quote.side == "BID" else "BUY"
    relevant = [
        trade
        for trade in trades
        if trade.asset_id == quote.token_id and trade.side.upper() == relevant_side
    ]
    at_quote = sum(
        trade.size_shares for trade in relevant if math.isclose(trade.price, quote.price, abs_tol=1e-12)
    )
    if quote.side == "BID":
        through = sum(trade.size_shares for trade in relevant if trade.price < quote.price)
        touched = None if final_midpoint is None else final_midpoint <= quote.price
    else:
        through = sum(trade.size_shares for trade in relevant if trade.price > quote.price)
        touched = None if final_midpoint is None else final_midpoint >= quote.price

    depletion = None
    persisted = None
    if final_depth_at_quote_shares is not None:
        depletion = max(0.0, quote.displayed_depth_at_quote_shares - final_depth_at_quote_shares)
        persisted = final_depth_at_quote_shares > 0

    reasons = []
    if through > 0:
        state = "TRADED_THROUGH"
        reasons.append("public aggressive-side trade printed beyond hypothetical quote")
    elif at_quote >= quote.displayed_depth_ahead_shares + quote.size_shares:
        state = "PROBABLE_FILL"
        reasons.append("public at-quote volume covered displayed queue-ahead plus hypothetical size")
    elif at_quote > 0:
        state = "POSSIBLE_FILL"
        reasons.append("public trade printed at quote but did not clear conservative queue-ahead requirement")
    elif depletion is not None and depletion > 0:
        state = "POSSIBLE_FILL"
        reasons.append("displayed quote depth depleted; cancellation versus execution is unresolved")
    else:
        state = "NO_FILL_EVIDENCE"
        reasons.append("no qualifying public trade or displayed-depth depletion")
    if touched and at_quote == 0 and through == 0:
        reasons.append("price touch alone is not treated as fill evidence")

    return SideFillEvidence(
        quote.side,
        state,
        round(at_quote, 12),
        round(through, 12),
        quote.displayed_depth_ahead_shares,
        final_depth_at_quote_shares,
        None if depletion is None else round(depletion, 12),
        persisted,
        touched,
        tuple(reasons),
    )


def combine_fill_evidence(
    bid: SideFillEvidence,
    ask: SideFillEvidence,
    *,
    quote_size_shares: float,
    observed_trade_count: int,
    followup_window_seconds: float,
    evidence_source: str = "public_clob_book_plus_public_data_api_trades",
    evidence_error: str | None = None,
) -> MakerFillFollowup:
    bid_strong = bid.state in STRONG_STATES
    ask_strong = ask.state in STRONG_STATES
    if bid.state == "UNKNOWN" or ask.state == "UNKNOWN":
        completion = "UNKNOWN"
    elif bid_strong and ask_strong:
        completion = "TWO_SIDED_PROBABLE"
    else:
        completion = "NO_TWO_SIDED_FILL_EVIDENCE"

    if bid_strong and not ask_strong:
        inventory_state = "ONE_SIDED_LONG_RISK"
        inventory = quote_size_shares
    elif ask_strong and not bid_strong:
        inventory_state = "ONE_SIDED_SHORT_RISK"
        inventory = -quote_size_shares
    elif bid_strong and ask_strong:
        inventory_state = "TWO_SIDED_COMPLETION_EVIDENCE"
        inventory = 0.0
    elif bid.state == "UNKNOWN" or ask.state == "UNKNOWN":
        inventory_state = "UNKNOWN"
        inventory = 0.0
    else:
        inventory_state = "NO_STRONG_ONE_SIDED_FILL_EVIDENCE"
        inventory = 0.0

    return MakerFillFollowup(
        bid,
        ask,
        completion,
        inventory_state,
        inventory,
        observed_trade_count,
        followup_window_seconds,
        evidence_source,
        evidence_error,
    )


def conservative_fill_probability(
    *, complete_evidence_trials: int, observed_trials: int, min_trials: int = 30, z: float = 1.96
) -> tuple[float | None, str]:
    if observed_trials < min_trials:
        return None, "UNKNOWN_INSUFFICIENT_EMPIRICAL_TRIALS"
    if not 0 <= complete_evidence_trials <= observed_trials:
        return None, "UNKNOWN_INVALID_EMPIRICAL_TRIALS"
    n = float(observed_trials)
    p = complete_evidence_trials / n
    denominator = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    lower = max(0.0, (centre - margin) / denominator)
    return round(lower, 12), "MEASURED_WILSON_LOWER_BOUND_OF_STRONG_PUBLIC_EVIDENCE"
