from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class SystemSnapshot:
    runtime_status: str = "UNAVAILABLE"
    launchd_pid: int | None = None
    release_sha: str = "unknown"
    last_completed_cycle: datetime | None = None
    cycle_age_seconds: float | None = None
    next_expected_cycle: datetime | None = None
    cycle_state: str = "UNKNOWN"
    database_status: str = "UNAVAILABLE"
    error_count: int = 0
    current_time: datetime | None = None
    message: str | None = None


@dataclass(frozen=True)
class PortfolioSnapshot:
    starting_balance: float = 0.0
    cash: float = 0.0
    deployed_capital: float = 0.0
    equity: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    modeled_open_ev: float = 0.0
    maximum_drawdown: float = 0.0
    open_positions: int = 0
    resolved_positions: int = 0
    capital_utilization: float = 0.0


@dataclass(frozen=True)
class PositionSnapshot:
    id: int
    question: str
    market_id: str
    category: str
    side: str
    stake: float
    entry_timestamp: datetime | None
    entry_bid: float | None
    entry_ask: float | None
    entry_price: float
    current_bid: float | None
    current_ask: float | None
    current_mark: float | None
    gross_pnl: float | None
    fees: float
    slippage: float
    net_pnl: float | None
    shares: float
    model_probability: float
    market_probability: float
    raw_edge: float
    executable_edge: float
    modeled_ev: float
    spread: float
    expected_holding_days: float | None
    expected_resolution: datetime | None
    source_forecast: str
    admission_reason: str
    status: str
    quote_timestamp: datetime | None
    quote_age_seconds: float | None
    quote_state: str


@dataclass(frozen=True)
class PipelineSnapshot:
    discovered_markets: int = 0
    eligible_markets: int = 0
    watchlist_size: int = 0
    fetched: int = 0
    ranked: int = 0
    evaluated: int = 0
    llm_calls: int = 0
    skeptic_calls: int = 0
    strict_candidates: int = 0
    revenue_candidates: int = 0
    revenue_admissions: int = 0
    cache_hits: int = 0
    budget_skips: int = 0
    rejections: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ApiSnapshot:
    daily_budget: float = 0.0
    estimated_spend_today: float | None = None
    remaining_budget: float | None = None
    calls_today: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_tokens: int = 0
    calls_avoided: int = 0
    cost_per_evaluation: float | None = None
    cost_per_candidate: float | None = None
    cost_per_admitted_trade: float | None = None
    unknown_historical_calls: int = 0
    measurement: str = "unknown"


@dataclass(frozen=True)
class EventSnapshot:
    timestamp: datetime | None
    kind: str
    message: str
    level: str = "INFO"


@dataclass(frozen=True)
class EvaluationSnapshot:
    timestamp: datetime | None
    market_id: str
    question: str
    decision: str
    reason: str
    executable_edge: float | None
    expected_value: float | None


@dataclass(frozen=True)
class ConsoleSnapshot:
    system: SystemSnapshot = field(default_factory=SystemSnapshot)
    portfolio: PortfolioSnapshot = field(default_factory=PortfolioSnapshot)
    positions: tuple[PositionSnapshot, ...] = ()
    pipeline: PipelineSnapshot = field(default_factory=PipelineSnapshot)
    api: ApiSnapshot = field(default_factory=ApiSnapshot)
    events: tuple[EventSnapshot, ...] = ()
    evaluations: tuple[EvaluationSnapshot, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)
