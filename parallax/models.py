from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Venue(StrEnum):
    POLYMARKET = "POLYMARKET"
    KALSHI = "KALSHI"


class Side(StrEnum):
    YES = "YES"
    NO = "NO"


class Action(StrEnum):
    BUY = "BUY"
    WATCH = "WATCH"
    PASS = "PASS"


class Confidence(StrEnum):
    ELITE = "ELITE"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    PASS = "PASS"


class PlayType(StrEnum):
    PARALLAX_EDGE = "PARALLAX_EDGE"
    PARALLAX_REPRICE = "PARALLAX_REPRICE"
    PARALLAX_VALUE = "PARALLAX_VALUE"
    PARALLAX_AVOID = "PARALLAX_AVOID"


class SignalType(StrEnum):
    PRICE_MOVE = "PRICE_MOVE"
    SPREAD_MOVE = "SPREAD_MOVE"
    LIQUIDITY_MOVE = "LIQUIDITY_MOVE"


class SignalSignificance(StrEnum):
    MATERIAL = "MATERIAL"
    HIGH = "HIGH"


def utcnow() -> datetime:
    return datetime.now(UTC)


def timestamp(value: Any) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.astimezone(UTC) if dt.tzinfo else None
    except (ValueError, TypeError):
        return None


@dataclass(frozen=True)
class Mechanics:
    quantity_step: float | None = None
    minimum_quantity: float | None = None
    price_ranges: tuple[tuple[float, float, float], ...] = ()
    payout: float | None = None
    fee_rate: float | None = None
    fee_rounding: str = "HALF_EVEN"
    fee_source: str = "Unknown"
    fee_valid_until: str | None = None
    fee_status: str = "REVIEWED"
    fee_observed_at: str | None = None
    fee_buffer_per_contract: float = 0
    fee_balance_precision: float = 0.01


@dataclass(frozen=True)
class NormalizedMarket:
    venue: Venue
    venue_market_id: str
    slug: str
    title: str
    description: str
    category: str
    event: str
    outcomes: dict[str, str]
    resolution_rules: str
    resolution_time: str | None
    status: str
    yes_bid: float | None
    yes_ask: float | None
    no_bid: float | None
    no_ask: float | None
    best_bid_size: float
    best_ask_size: float
    executable_depth: dict[str, tuple[tuple[float, float], ...]]
    recent_volume: float | None
    recent_trade_count: int | None
    last_trade_time: str | None
    book_timestamp: str | None
    data_timestamp: str
    source_url: str | None
    mechanics: Mechanics
    original_metadata: dict[str, Any] = field(default_factory=dict)
    timestamp_basis: str = "source"
    demo: bool = False
    event_title: str | None = None


@dataclass(frozen=True)
class Evidence:
    """Reviewed model output; never inferred from a market's midpoint.

    Probability describes YES. Score below describes qualification, not accuracy.
    Bind evidence to exact venue, market and reviewed rules; expire it explicitly.
    """

    venue: Venue
    market_id: str
    fair_probability: float
    source: str
    model_version: str
    observed_at: str
    valid_until: str
    rules_digest: str
    review_reference: str
    rationale: str
    independent_sources: tuple[str, ...] = ()
    validation_reference: str = ""
    contradictions: tuple[str, ...] = ()
    invalidated: bool = False
    new_information: bool = False
    demo: bool = False
    play_type: PlayType = PlayType.PARALLAX_EDGE


@dataclass(frozen=True)
class RetailExample:
    stake: float
    available: bool
    reason: str | None
    contracts_or_shares: float = 0
    amount_spent: float = 0
    unspent: float = 0
    estimated_payout_if_correct: float = 0
    estimated_profit_if_correct: float = 0
    maximum_loss: float = 0
    fees_estimate: float | None = None
    slippage_estimate: float = 0
    total_cost: float | None = None
    net_profit_if_correct: float | None = None
    maximum_loss_including_fees: float | None = None


@dataclass(frozen=True)
class Verdict:
    action: Action
    primary_reason: str
    supporting_reasons: tuple[str, ...]
    risk_reasons: tuple[str, ...]
    invalidation_conditions: tuple[str, ...]
    failed_gates: tuple[str, ...]


@dataclass(frozen=True)
class ParallaxPlay:
    id: str
    created_at: str
    updated_at: str
    expires_at: str
    venue: Venue
    market_id: str
    market_title: str
    market_url: str | None
    market_reference: str
    side: Side
    side_description: str
    play_type: PlayType
    current_price: float | None
    parallax_fair_value: float | None
    edge_points: float | None
    confidence_score: float
    confidence_band: Confidence
    confidence_method: str
    liquidity_score: float
    data_freshness: str
    suggested_action: Action
    reason_summary: str
    reason_factors: tuple[str, ...]
    risk_factors: tuple[str, ...]
    resolution_time: str | None
    estimated_time_to_resolution: float | None
    retail_examples: tuple[RetailExample, ...]
    executable_price: float | None
    executable_size: float
    max_reasonable_retail_size: float
    fees_estimate: float | None
    slippage_estimate: float | None
    expected_value: float | None
    expected_return: float | None
    market_probability: float | None
    model_probability: float | None
    invalidation_conditions: tuple[str, ...]
    decision_reasons: tuple[str, ...]
    verdict: Verdict
    status: str
    demo: bool
    evidence: Evidence | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ParallaxSignal:
    id: str
    detected_at: str
    venue: Venue
    market_id: str
    market_title: str
    signal_type: SignalType
    side: Side
    previous_value: float
    current_value: float
    absolute_change: float
    percent_change: float | None
    observation_window_seconds: float
    significance: SignalSignificance
    explanation: str
    market_url: str | None
    market_reference: str
    resolution_time: str | None
    event_title: str | None = None
    display_title: str | None = None
    category: str | None = None
    direction: str | None = None
    signal_label: str | None = None
    signal_strength: str | None = None
    formatted_previous_value: str | None = None
    formatted_current_value: str | None = None
    formatted_change: str | None = None
    formatted_window: str | None = None
    resolution_label: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
