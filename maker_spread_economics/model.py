from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


UNKNOWN = "UNKNOWN"
PAPER_ONLY = "PAPER_ONLY"


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 12)


@dataclass(frozen=True)
class QuoteSnapshot:
    market_id: str
    token_id: str
    observed_at_utc: str
    best_bid: float
    best_ask: float
    bid_size_shares: float
    ask_size_shares: float
    source_book_timestamp_utc: str | None = None

    def __post_init__(self) -> None:
        values = (self.best_bid, self.best_ask, self.bid_size_shares, self.ask_size_shares)
        if not self.market_id or not self.token_id:
            raise ValueError("market_id and token_id are required")
        if not all(math.isfinite(value) for value in values):
            raise ValueError("quote values must be finite")
        if not 0 < self.best_bid < self.best_ask < 1:
            raise ValueError("quote requires a valid positive two-sided spread")
        if self.bid_size_shares <= 0 or self.ask_size_shares <= 0:
            raise ValueError("quote requires positive displayed depth")
        if _utc(self.observed_at_utc) is None:
            raise ValueError("observed_at_utc must be timezone-aware ISO-8601")
        if self.source_book_timestamp_utc is not None and _utc(self.source_book_timestamp_utc) is None:
            raise ValueError("source_book_timestamp_utc must be timezone-aware ISO-8601")

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


@dataclass(frozen=True)
class MakerFeeMetadata:
    rate: float | None
    exponent: float | None
    taker_only: bool | None
    rebate_rate: float | None
    source: str
    authoritative: bool

    @property
    def calculable(self) -> bool:
        return bool(
            self.authoritative
            and self.rate is not None
            and self.exponent is not None
            and self.taker_only is not None
            and self.rebate_rate is not None
            and self.rate >= 0
            and self.exponent >= 0
            and 0 <= self.rebate_rate <= 1
        )


@dataclass(frozen=True)
class MakerFillEvidence:
    complete_two_sided_fills: int
    quote_attempts: int
    source: str


@dataclass(frozen=True)
class MakerValidationConfig:
    target_shares: float = 5.0
    latency_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_shares) or self.target_shares <= 0:
            raise ValueError("target_shares must be finite and positive")
        if not math.isfinite(self.latency_seconds) or self.latency_seconds < 0:
            raise ValueError("latency_seconds must be finite and non-negative")


@dataclass(frozen=True)
class MakerEvaluation:
    market_id: str
    token_id: str
    execution_mode: str
    status: str
    economic_status: str
    rejection_reason: str | None
    signal_observed_at_utc: str
    activation_observed_at_utc: str
    post_quote_observed_at_utc: str
    configured_latency_seconds: float
    observed_latency_seconds: float
    quote_survived_latency: bool
    quote_observation_seconds: float
    quote_persisted_through_observation: bool
    quote_lifetime_lower_bound_seconds: float
    quote_lifetime_upper_bound_seconds: float | None
    best_bid: float
    best_ask: float
    spread_per_share: float
    displayed_bid_depth_shares: float
    displayed_ask_depth_shares: float
    displayed_bid_depth_usd: float
    displayed_ask_depth_usd: float
    conditional_size_shares: float
    paper_filled_shares: float
    captured_spread_usd: float | None
    maker_rebate_usd: float | None
    applicable_maker_fees_usd: float | None
    adverse_selection_movement_per_share: float
    adverse_selection_cost_usd: float | None
    conditional_maker_edge_usd: float | None
    fill_probability: float | None
    fill_probability_status: str
    fill_adjusted_expected_edge_usd: float | None
    fee_metadata: MakerFeeMetadata
    measured_inputs: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_unknowns: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fee_metadata_from_venue(
    market: dict[str, Any], *, clob_market_info: dict[str, Any] | None = None
) -> MakerFeeMetadata:
    schedule = market.get("fee_schedule") or market.get("feeSchedule")
    if isinstance(schedule, dict):
        rate = _finite(schedule.get("rate"))
        exponent = _finite(schedule.get("exponent", schedule.get("fee_exponent")))
        taker_only = schedule.get("taker_only", schedule.get("takerOnly"))
        rebate = _finite(schedule.get("rebate_rate", schedule.get("rebateRate")))
        if rate is not None and exponent is not None and taker_only is not None and rebate is not None:
            return MakerFeeMetadata(
                rate, exponent, bool(taker_only), rebate, "polymarket_market_fee_schedule", True
            )

    info = clob_market_info if isinstance(clob_market_info, dict) else {}
    details = info.get("fd") or info.get("fee_details") or info.get("feeDetails")
    if isinstance(details, dict):
        rate = _finite(details.get("r", details.get("rate")))
        exponent = _finite(details.get("e", details.get("exponent")))
        taker_only = details.get("to", details.get("taker_only", details.get("takerOnly")))
        rebate = _finite(details.get("rr", details.get("rebate_rate", details.get("rebateRate"))))
        if rate is not None and exponent is not None and taker_only is not None and rebate is not None:
            return MakerFeeMetadata(
                rate, exponent, bool(taker_only), rebate, "polymarket_clob_market_info.fd", True
            )

    source = "UNKNOWN_MAKER_REBATE_METADATA"
    if schedule or details or market.get("feesEnabled") is False:
        source = "polymarket_fee_metadata_missing_maker_rebate"
    return MakerFeeMetadata(None, None, None, None, source, False)


def _fee_curve(shares: float, price: float, metadata: MakerFeeMetadata) -> float:
    return shares * float(metadata.rate) * (price * (1.0 - price)) ** float(metadata.exponent)


def _fill_probability(evidence: MakerFillEvidence | None) -> tuple[float | None, str]:
    if evidence is None:
        return None, "UNKNOWN_NO_EMPIRICAL_FILL_EVIDENCE"
    if not evidence.source.strip():
        return None, "UNKNOWN_UNSOURCED_FILL_EVIDENCE"
    if evidence.quote_attempts <= 0:
        return None, "UNKNOWN_NO_QUOTE_ATTEMPTS"
    if not 0 <= evidence.complete_two_sided_fills <= evidence.quote_attempts:
        return None, "UNKNOWN_INVALID_FILL_EVIDENCE"
    return evidence.complete_two_sided_fills / evidence.quote_attempts, "MEASURED_EMPIRICAL_COMPLETE_FILL_RATE"


def evaluate_maker_quote(
    *,
    signal_quote: QuoteSnapshot,
    activation_quote: QuoteSnapshot,
    post_quote: QuoteSnapshot,
    fee_metadata: MakerFeeMetadata,
    config: MakerValidationConfig,
    fill_evidence: MakerFillEvidence | None = None,
) -> MakerEvaluation:
    if len({signal_quote.market_id, activation_quote.market_id, post_quote.market_id}) != 1:
        raise ValueError("all quote observations must refer to the same market")
    if len({signal_quote.token_id, activation_quote.token_id, post_quote.token_id}) != 1:
        raise ValueError("all quote observations must refer to the same token")

    signal_at = _utc(signal_quote.observed_at_utc)
    activation_at = _utc(activation_quote.observed_at_utc)
    post_at = _utc(post_quote.observed_at_utc)
    assert signal_at is not None and activation_at is not None and post_at is not None
    observed_latency = (activation_at - signal_at).total_seconds()
    quote_observation = (post_at - activation_at).total_seconds()
    if observed_latency < 0 or quote_observation < 0:
        raise ValueError("quote observations must be chronological")

    survived = (
        signal_quote.best_bid == activation_quote.best_bid
        and signal_quote.best_ask == activation_quote.best_ask
    )
    persisted = (
        activation_quote.best_bid == post_quote.best_bid
        and activation_quote.best_ask == post_quote.best_ask
    )
    size = min(
        config.target_shares,
        activation_quote.bid_size_shares,
        activation_quote.ask_size_shares,
    )
    spread = activation_quote.best_ask - activation_quote.best_bid
    adverse_per_share = abs(post_quote.midpoint - activation_quote.midpoint)
    fill_probability, fill_status = _fill_probability(fill_evidence)

    captured = rebate = maker_fees = adverse_cost = conditional_edge = expected_edge = None
    rejection_reason = None
    if observed_latency + 1e-9 < config.latency_seconds:
        status = "UNKNOWN_LATENCY_NOT_OBSERVED"
        economic_status = UNKNOWN
        rejection_reason = "configured latency was not observed"
    elif not survived:
        status = "REJECTED_STALE_QUOTE_AFTER_LATENCY"
        economic_status = "STALE_BEFORE_HYPOTHETICAL_QUOTE_ACTIVATION"
        rejection_reason = "top-of-book prices changed during configured latency"
    elif not fee_metadata.calculable:
        status = "UNKNOWN_MAKER_REBATE_ECONOMICS"
        economic_status = UNKNOWN
        rejection_reason = "authoritative maker fee/rebate metadata unavailable"
    else:
        captured = size * spread
        bid_curve = _fee_curve(size, activation_quote.best_bid, fee_metadata)
        ask_curve = _fee_curve(size, activation_quote.best_ask, fee_metadata)
        rebate = (bid_curve + ask_curve) * float(fee_metadata.rebate_rate)
        maker_fees = 0.0 if fee_metadata.taker_only else bid_curve + ask_curve
        adverse_cost = size * adverse_per_share
        conditional_edge = captured + rebate - adverse_cost - maker_fees
        if conditional_edge <= 0:
            status = "REJECTED_CONDITIONAL_MAKER_EDGE"
            economic_status = "NON_POSITIVE_CONDITIONAL_ON_COMPLETE_TWO_SIDED_FILL"
            rejection_reason = "adverse selection and applicable fees exceed captured spread and rebate"
        elif fill_probability is None:
            status = "CONDITIONAL_EDGE_FILL_UNKNOWN"
            economic_status = "POSITIVE_ONLY_IF_COMPLETE_TWO_SIDED_MAKER_FILL"
            rejection_reason = "fill probability is unsupported"
        else:
            status = "MEASURED_FILL_CONDITIONAL_EDGE"
            economic_status = "FILL_ADJUSTED_PAPER_ESTIMATE"
            expected_edge = conditional_edge * fill_probability

    unresolved = []
    if not fee_metadata.calculable:
        unresolved.append("maker fee/rebate economics")
    if fill_probability is None:
        unresolved.extend(("maker queue position", "complete two-sided maker fill probability"))
    if not persisted:
        unresolved.append("exact quote lifetime within observation window")

    return MakerEvaluation(
        market_id=activation_quote.market_id,
        token_id=activation_quote.token_id,
        execution_mode=PAPER_ONLY,
        status=status,
        economic_status=economic_status,
        rejection_reason=rejection_reason,
        signal_observed_at_utc=signal_quote.observed_at_utc,
        activation_observed_at_utc=activation_quote.observed_at_utc,
        post_quote_observed_at_utc=post_quote.observed_at_utc,
        configured_latency_seconds=config.latency_seconds,
        observed_latency_seconds=_rounded(observed_latency),
        quote_survived_latency=survived,
        quote_observation_seconds=_rounded(quote_observation),
        quote_persisted_through_observation=persisted,
        quote_lifetime_lower_bound_seconds=_rounded(quote_observation if persisted else 0.0),
        quote_lifetime_upper_bound_seconds=None if persisted else _rounded(quote_observation),
        best_bid=activation_quote.best_bid,
        best_ask=activation_quote.best_ask,
        spread_per_share=_rounded(spread),
        displayed_bid_depth_shares=activation_quote.bid_size_shares,
        displayed_ask_depth_shares=activation_quote.ask_size_shares,
        displayed_bid_depth_usd=_rounded(activation_quote.best_bid * activation_quote.bid_size_shares),
        displayed_ask_depth_usd=_rounded(activation_quote.best_ask * activation_quote.ask_size_shares),
        conditional_size_shares=_rounded(size),
        paper_filled_shares=0.0,
        captured_spread_usd=_rounded(captured),
        maker_rebate_usd=_rounded(rebate),
        applicable_maker_fees_usd=_rounded(maker_fees),
        adverse_selection_movement_per_share=_rounded(adverse_per_share),
        adverse_selection_cost_usd=_rounded(adverse_cost),
        conditional_maker_edge_usd=_rounded(conditional_edge),
        fill_probability=_rounded(fill_probability),
        fill_probability_status=fill_status,
        fill_adjusted_expected_edge_usd=_rounded(expected_edge),
        fee_metadata=fee_metadata,
        measured_inputs=(
            "signal/activation/post public CLOB top bid and ask",
            "displayed top-level bid and ask depth",
            "venue fee/rebate metadata",
            "configured-latency quote survival",
            "post-quote absolute midpoint movement adverse-selection proxy",
            "quote persistence observation window",
        ),
        assumptions=(
            "conditional two-sided maker fill captures activation spread",
            "absolute post-quote midpoint movement is charged as adverse-selection cost",
            "displayed depth does not establish queue priority or fill",
        ),
        unresolved_unknowns=tuple(unresolved),
    )
