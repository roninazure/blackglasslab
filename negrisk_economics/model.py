from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence


UNKNOWN = "UNKNOWN"
PAPER_ONLY = "PAPER_ONLY"


def _finite(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 12)


def _utc(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class BookLevel:
    price: float
    shares: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.price) or not 0 < self.price < 1:
            raise ValueError("ask price must be finite and between zero and one")
        if not math.isfinite(self.shares) or self.shares <= 0:
            raise ValueError("ask shares must be finite and positive")


@dataclass(frozen=True)
class FeeMetadata:
    rate: float | None
    exponent: float | None
    taker_only: bool | None
    source: str
    authoritative: bool

    @property
    def calculable(self) -> bool:
        return bool(
            self.authoritative
            and self.rate is not None
            and self.exponent is not None
            and self.rate >= 0
            and self.exponent >= 0
        )


@dataclass(frozen=True)
class LegBook:
    market_id: str
    token_id: str
    observed_at_utc: str
    asks: tuple[BookLevel, ...]
    fee: FeeMetadata
    source_book_timestamp_utc: str | None = None

    def __post_init__(self) -> None:
        if not self.market_id or not self.token_id:
            raise ValueError("market_id and token_id are required")
        if len(self.asks) == 0:
            raise ValueError("each leg requires observable ask depth")
        if _utc(self.observed_at_utc) is None:
            raise ValueError("observed_at_utc must be timezone-aware ISO-8601")
        if self.source_book_timestamp_utc is not None and _utc(self.source_book_timestamp_utc) is None:
            raise ValueError("source_book_timestamp_utc must be timezone-aware ISO-8601")
        object.__setattr__(self, "asks", tuple(sorted(self.asks, key=lambda row: row.price)))

    @property
    def visible_shares(self) -> float:
        return sum(level.shares for level in self.asks)


@dataclass(frozen=True)
class FillEvidence:
    basket_fills: int
    basket_attempts: int
    source: str


@dataclass(frozen=True)
class ValidationConfig:
    target_shares: float = 5.0
    min_shares: float = 5.0
    latency_seconds: float = 1.0
    allow_depth_limited_size: bool = True
    min_net_edge_usd: float = 0.0
    min_fill_trials: int = 30
    fill_confidence_z: float = 1.96

    def __post_init__(self) -> None:
        if not math.isfinite(self.target_shares) or self.target_shares <= 0:
            raise ValueError("target_shares must be finite and positive")
        if not math.isfinite(self.min_shares) or self.min_shares <= 0:
            raise ValueError("min_shares must be finite and positive")
        if self.min_shares > self.target_shares:
            raise ValueError("min_shares cannot exceed target_shares")
        if not math.isfinite(self.latency_seconds) or self.latency_seconds < 0:
            raise ValueError("latency_seconds must be finite and non-negative")
        if not math.isfinite(self.min_net_edge_usd):
            raise ValueError("min_net_edge_usd must be finite")
        if self.min_fill_trials < 1:
            raise ValueError("min_fill_trials must be positive")
        if not math.isfinite(self.fill_confidence_z) or self.fill_confidence_z <= 0:
            raise ValueError("fill_confidence_z must be finite and positive")


@dataclass(frozen=True)
class LegEconomics:
    market_id: str
    token_id: str
    top_ask: float
    filled_shares: float
    top_cost_usd: float
    walked_cost_usd: float
    slippage_usd: float
    fee_usd: float | None
    fee_source: str
    observed_at_utc: str
    source_book_timestamp_utc: str | None


@dataclass(frozen=True)
class BasketEvaluation:
    event_id: str
    execution_mode: str
    status: str
    economic_status: str
    rejection_reason: str | None
    signal_observed_at_utc: str | None
    execution_observed_at_utc: str | None
    configured_latency_seconds: float
    observed_latency_seconds: float | None
    opportunity_survived_latency: bool | None
    requested_shares: float
    simultaneous_depth_shares: float | None
    simultaneous_depth_usd: float | None
    sized_shares: float
    paper_filled_shares: float
    signal_gross_edge_per_share: float | None
    gross_edge_usd: float | None
    top_of_book_cost_usd: float | None
    walked_cost_usd: float | None
    slippage_usd: float | None
    fee_usd: float | None
    net_executable_edge_usd: float | None
    fill_probability: float | None
    fill_probability_status: str
    fill_adjusted_net_edge_usd: float | None
    partial_fill_worst_case_loss_usd: float | None
    legs: tuple[LegEconomics, ...]
    measured_inputs: tuple[str, ...]
    assumptions: tuple[str, ...]
    unresolved_unknowns: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def fee_metadata_from_venue(
    market: dict[str, Any],
    *,
    clob_market_info: dict[str, Any] | None = None,
    token_fee: dict[str, Any] | None = None,
) -> FeeMetadata:
    """Read fee parameters without filling gaps from a category/default schedule.

    CLOB V2 ``fd`` metadata is preferred because it supplies the rate and curve
    exponent used by the official SDK. Explicit zero-fee venue metadata is also
    sufficient. A non-zero legacy base fee without an exponent remains UNKNOWN.
    """

    info = clob_market_info if isinstance(clob_market_info, dict) else {}
    details = info.get("fd") or info.get("fee_details") or info.get("feeDetails")
    if isinstance(details, dict):
        rate = _finite(details.get("r", details.get("rate", details.get("fee_rate"))))
        exponent = _finite(details.get("e", details.get("exponent", details.get("fee_exponent"))))
        taker_only = details.get("to", details.get("taker_only", details.get("takerOnly")))
        if rate is not None and exponent is not None:
            return FeeMetadata(rate, exponent, bool(taker_only), "polymarket_clob_market_info.fd", True)

    schedule = market.get("fee_schedule") or market.get("feeSchedule")
    if isinstance(schedule, dict):
        rate = _finite(schedule.get("rate"))
        exponent = _finite(schedule.get("exponent", schedule.get("fee_exponent")))
        if rate is not None and exponent is not None:
            return FeeMetadata(
                rate,
                exponent,
                bool(schedule.get("taker_only", schedule.get("takerOnly", True))),
                "polymarket_market_fee_schedule",
                True,
            )

    if market.get("feesEnabled") is False:
        return FeeMetadata(0.0, 0.0, True, "polymarket_fees_disabled", True)

    taker_bps = _finite(info.get("tbf"))
    if taker_bps == 0:
        return FeeMetadata(0.0, 0.0, True, "polymarket_clob_market_info.tbf_zero", True)
    base_bps = _finite((token_fee or {}).get("base_fee"))
    if base_bps == 0:
        return FeeMetadata(0.0, 0.0, True, "polymarket_token_fee_zero", True)

    source = "UNKNOWN"
    if taker_bps is not None or base_bps is not None or market.get("takerBaseFee") is not None:
        source = "polymarket_base_fee_missing_curve_exponent"
    return FeeMetadata(None, None, None, source, False)


def _walk(book: LegBook, shares: float) -> tuple[float, float, float, float | None]:
    remaining = shares
    cost = 0.0
    fee = 0.0 if book.fee.calculable else None
    for level in book.asks:
        take = min(remaining, level.shares)
        cost += take * level.price
        if fee is not None:
            # Matches Polymarket's CLOB V2 SDK: shares * rate * (p*(1-p))**exponent.
            fee += take * float(book.fee.rate) * (level.price * (1.0 - level.price)) ** float(book.fee.exponent)
        remaining -= take
        if remaining <= 1e-12:
            break
    if remaining > 1e-9:
        raise ValueError("requested shares exceed visible leg depth")
    top_cost = shares * book.asks[0].price
    return cost, top_cost, max(0.0, cost - top_cost), fee


def conservative_fill_probability(evidence: FillEvidence | None, config: ValidationConfig) -> tuple[float | None, str]:
    if evidence is None:
        return None, "UNKNOWN_NO_EMPIRICAL_FILL_EVIDENCE"
    if not evidence.source.strip():
        return None, "UNKNOWN_UNSOURCED_FILL_EVIDENCE"
    if evidence.basket_attempts < config.min_fill_trials:
        return None, "UNKNOWN_INSUFFICIENT_FILL_TRIALS"
    if not 0 <= evidence.basket_fills <= evidence.basket_attempts:
        return None, "UNKNOWN_INVALID_FILL_EVIDENCE"
    n = float(evidence.basket_attempts)
    p = evidence.basket_fills / n
    z = config.fill_confidence_z
    denominator = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return _rounded(max(0.0, (centre - margin) / denominator)), "MEASURED_WILSON_LOWER_BOUND"


def _result(
    *,
    event_id: str,
    config: ValidationConfig,
    status: str,
    economic_status: str = UNKNOWN,
    rejection_reason: str | None,
    signal_at: str | None = None,
    execution_at: str | None = None,
    observed_latency: float | None = None,
    survived: bool | None = None,
    depth_shares: float | None = None,
    depth_usd: float | None = None,
    sized_shares: float = 0.0,
    paper_filled_shares: float = 0.0,
    signal_edge: float | None = None,
    gross: float | None = None,
    top_cost: float | None = None,
    walked_cost: float | None = None,
    slippage: float | None = None,
    fee: float | None = None,
    net: float | None = None,
    fill_probability: float | None = None,
    fill_status: str = "UNKNOWN_NO_EMPIRICAL_FILL_EVIDENCE",
    fill_adjusted: float | None = None,
    partial_loss: float | None = None,
    legs: Iterable[LegEconomics] = (),
    unknowns: Iterable[str] = (),
) -> BasketEvaluation:
    return BasketEvaluation(
        event_id=event_id,
        execution_mode=PAPER_ONLY,
        status=status,
        economic_status=economic_status,
        rejection_reason=rejection_reason,
        signal_observed_at_utc=signal_at,
        execution_observed_at_utc=execution_at,
        configured_latency_seconds=config.latency_seconds,
        observed_latency_seconds=_rounded(observed_latency),
        opportunity_survived_latency=survived,
        requested_shares=config.target_shares,
        simultaneous_depth_shares=_rounded(depth_shares),
        simultaneous_depth_usd=_rounded(depth_usd),
        sized_shares=_rounded(sized_shares) or 0.0,
        paper_filled_shares=_rounded(paper_filled_shares) or 0.0,
        signal_gross_edge_per_share=_rounded(signal_edge),
        gross_edge_usd=_rounded(gross),
        top_of_book_cost_usd=_rounded(top_cost),
        walked_cost_usd=_rounded(walked_cost),
        slippage_usd=_rounded(slippage),
        fee_usd=_rounded(fee),
        net_executable_edge_usd=_rounded(net),
        fill_probability=_rounded(fill_probability),
        fill_probability_status=fill_status,
        fill_adjusted_net_edge_usd=_rounded(fill_adjusted),
        partial_fill_worst_case_loss_usd=_rounded(partial_loss),
        legs=tuple(legs),
        measured_inputs=(
            "CLOB ask prices and displayed share depth for every basket leg",
            "CLOB/Gamma observation timestamps",
            "authoritative per-market fee curve metadata when available",
        ),
        assumptions=(
            "buy all YES legs as taker orders and hold a proven-complete mutually exclusive basket to its $1/share payout",
            "walk displayed asks from best to worst; displayed size remains available until the paper decision",
            "paper basket uses all-or-none full-basket acceptance although venue orders across markets are not atomic",
            "no builder fee because paper mode attaches no builder code; funding, custody, gas, and tax costs are outside the measured venue-cost model",
        ),
        unresolved_unknowns=tuple(unknowns),
    )


def evaluate_basket(
    *,
    event_id: str,
    initial_books: Sequence[LegBook],
    execution_books: Sequence[LegBook],
    basket_complete: bool | None,
    config: ValidationConfig = ValidationConfig(),
    fill_evidence: FillEvidence | None = None,
) -> BasketEvaluation:
    """Evaluate conditional full-basket edge. This function cannot place orders."""

    fill_probability, fill_status = conservative_fill_probability(fill_evidence, config)
    if basket_complete is not True:
        return _result(
            event_id=event_id,
            config=config,
            status="UNKNOWN_BASKET_COMPLETENESS",
            rejection_reason="complete_mutually_exclusive_outcome_set_not_proven",
            fill_probability=fill_probability,
            fill_status=fill_status,
            unknowns=("basket completeness", "fill probability") if fill_probability is None else ("basket completeness",),
        )

    initial = {book.token_id: book for book in initial_books}
    execution = {book.token_id: book for book in execution_books}
    if len(initial) < 3 or len(initial) != len(initial_books) or set(initial) != set(execution):
        return _result(
            event_id=event_id,
            config=config,
            status="REJECTED_INCOMPLETE_BASKET",
            rejection_reason="missing_duplicate_or_changed_basket_leg",
            fill_probability=fill_probability,
            fill_status=fill_status,
            unknowns=("fill probability",) if fill_probability is None else (),
        )

    signal_time = max(_utc(book.observed_at_utc) for book in initial.values())
    execution_time = min(_utc(book.observed_at_utc) for book in execution.values())
    assert signal_time is not None and execution_time is not None
    observed_latency = (execution_time - signal_time).total_seconds()
    signal_at = signal_time.isoformat().replace("+00:00", "Z")
    execution_at = execution_time.isoformat().replace("+00:00", "Z")
    signal_edge = 1.0 - sum(book.asks[0].price for book in initial.values())
    depth_shares = min(book.visible_shares for book in execution.values())
    depth_cost = sum(_walk(book, depth_shares)[0] for book in execution.values())

    if observed_latency + 1e-9 < config.latency_seconds:
        return _result(
            event_id=event_id,
            config=config,
            status="UNKNOWN_LATENCY_NOT_OBSERVED",
            rejection_reason="no_complete_basket_snapshot_at_configured_latency",
            signal_at=signal_at,
            execution_at=execution_at,
            observed_latency=observed_latency,
            depth_shares=depth_shares,
            depth_usd=depth_cost,
            signal_edge=signal_edge,
            fill_probability=fill_probability,
            fill_status=fill_status,
            unknowns=("opportunity survival at configured latency", "fill probability") if fill_probability is None else ("opportunity survival at configured latency",),
        )

    sized_shares = min(config.target_shares, depth_shares)
    if sized_shares + 1e-9 < config.min_shares:
        return _result(
            event_id=event_id,
            config=config,
            status="REJECTED_INSUFFICIENT_SIMULTANEOUS_DEPTH",
            rejection_reason="common_visible_depth_below_minimum_basket_size",
            signal_at=signal_at,
            execution_at=execution_at,
            observed_latency=observed_latency,
            survived=False,
            depth_shares=depth_shares,
            depth_usd=depth_cost,
            sized_shares=sized_shares,
            signal_edge=signal_edge,
            fill_probability=fill_probability,
            fill_status=fill_status,
            unknowns=("fill probability",) if fill_probability is None else (),
        )
    if depth_shares + 1e-9 < config.target_shares and not config.allow_depth_limited_size:
        return _result(
            event_id=event_id,
            config=config,
            status="REJECTED_PARTIAL_BASKET_RISK",
            rejection_reason="requested_size_not_available_on_every_leg",
            signal_at=signal_at,
            execution_at=execution_at,
            observed_latency=observed_latency,
            survived=False,
            depth_shares=depth_shares,
            depth_usd=depth_cost,
            sized_shares=sized_shares,
            signal_edge=signal_edge,
            fill_probability=fill_probability,
            fill_status=fill_status,
            unknowns=("fill probability",) if fill_probability is None else (),
        )

    leg_rows: list[LegEconomics] = []
    total_cost = top_cost = slippage = 0.0
    total_fee: float | None = 0.0
    partial_costs: list[float] = []
    for token_id in sorted(execution):
        book = execution[token_id]
        walked, at_top, impact, fee = _walk(book, sized_shares)
        total_cost += walked
        top_cost += at_top
        slippage += impact
        partial_costs.append(walked + (fee or 0.0))
        if fee is None:
            total_fee = None
        elif total_fee is not None:
            total_fee += fee
        leg_rows.append(
            LegEconomics(
                market_id=book.market_id,
                token_id=book.token_id,
                top_ask=_rounded(book.asks[0].price) or 0.0,
                filled_shares=_rounded(sized_shares) or 0.0,
                top_cost_usd=_rounded(at_top) or 0.0,
                walked_cost_usd=_rounded(walked) or 0.0,
                slippage_usd=_rounded(impact) or 0.0,
                fee_usd=_rounded(fee),
                fee_source=book.fee.source,
                observed_at_utc=book.observed_at_utc,
                source_book_timestamp_utc=book.source_book_timestamp_utc,
            )
        )

    gross = sized_shares - top_cost
    partial_loss = (
        sum(partial_costs) - min(partial_costs)
        if len(partial_costs) > 1 and total_fee is not None
        else None
    )
    if total_fee is None:
        return _result(
            event_id=event_id,
            config=config,
            status="UNKNOWN_FEE_ECONOMICS",
            rejection_reason="authoritative_fee_curve_missing_for_one_or_more_legs",
            signal_at=signal_at,
            execution_at=execution_at,
            observed_latency=observed_latency,
            survived=None,
            depth_shares=depth_shares,
            depth_usd=depth_cost,
            sized_shares=sized_shares,
            signal_edge=signal_edge,
            gross=gross,
            top_cost=top_cost,
            walked_cost=total_cost,
            slippage=slippage,
            partial_loss=partial_loss,
            legs=leg_rows,
            fill_probability=fill_probability,
            fill_status=fill_status,
            unknowns=("fee curve", "fee-inclusive partial-fill loss", "fill probability", "non-atomic live basket fill") if fill_probability is None else ("fee curve", "fee-inclusive partial-fill loss", "non-atomic live basket fill"),
        )

    net = sized_shares - total_cost - total_fee
    survived = bool(signal_edge > 0 and net > config.min_net_edge_usd)
    fill_adjusted = net * fill_probability if fill_probability is not None else None
    status = "PAPER_CANDIDATE" if survived else "REJECTED_NET_EDGE"
    economic_status = "POSITIVE_AFTER_MEASURED_FEES_AND_BOOK_SLIPPAGE" if survived else "NON_POSITIVE_AFTER_MEASURED_FEES_AND_BOOK_SLIPPAGE"
    rejection = None if survived else "opportunity_did_not_survive_latency_and_execution_costs"
    return _result(
        event_id=event_id,
        config=config,
        status=status,
        economic_status=economic_status,
        rejection_reason=rejection,
        signal_at=signal_at,
        execution_at=execution_at,
        observed_latency=observed_latency,
        survived=survived,
        depth_shares=depth_shares,
        depth_usd=depth_cost,
        sized_shares=sized_shares,
        paper_filled_shares=sized_shares if survived else 0.0,
        signal_edge=signal_edge,
        gross=gross,
        top_cost=top_cost,
        walked_cost=total_cost,
        slippage=slippage,
        fee=total_fee,
        net=net,
        fill_probability=fill_probability,
        fill_status=fill_status,
        fill_adjusted=fill_adjusted,
        partial_loss=partial_loss,
        legs=leg_rows,
        unknowns=("fill probability", "non-atomic live basket fill") if fill_probability is None else ("non-atomic live basket fill",),
    )
