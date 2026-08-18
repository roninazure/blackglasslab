from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Any


def _number(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _integer(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RevenueConfig:
    starting_balance_usd: float = 1000.0
    position_size_usd: float = 25.0
    max_open_positions: int = 20
    max_capital_deployed_usd: float = 500.0
    max_category_positions: int = 5
    min_executable_edge: float = 0.02
    daily_api_budget_usd: float = 2.0
    estimated_api_cost_per_call_usd: float = 0.0
    fee_bps: float = 0.0
    slippage_bps: float = 10.0
    max_quote_age_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> "RevenueConfig":
        config = cls(
            starting_balance_usd=_number("BGL_REVENUE_STARTING_BALANCE_USD", 1000.0),
            position_size_usd=_number("BGL_REVENUE_POSITION_SIZE_USD", 25.0),
            max_open_positions=_integer("BGL_REVENUE_MAX_OPEN_POSITIONS", 20),
            max_capital_deployed_usd=_number("BGL_REVENUE_MAX_DEPLOYED_USD", 500.0),
            max_category_positions=_integer("BGL_REVENUE_MAX_CATEGORY_POSITIONS", 5),
            min_executable_edge=_number("BGL_REVENUE_MIN_EXECUTABLE_EDGE", 0.02),
            daily_api_budget_usd=_number("BGL_REVENUE_DAILY_API_BUDGET_USD", 2.0),
            estimated_api_cost_per_call_usd=_number(
                "BGL_ESTIMATED_COST_PER_CALL_USD", 0.0
            ),
            fee_bps=_number("BGL_REVENUE_FEE_BPS", 0.0),
            slippage_bps=_number("BGL_REVENUE_SLIPPAGE_BPS", 10.0),
            max_quote_age_seconds=_number(
                "BGL_REVENUE_MAX_QUOTE_AGE_SECONDS", 5.0
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.starting_balance_usd <= 0 or self.position_size_usd <= 0:
            raise ValueError("revenue balances and position size must be positive")
        if self.max_open_positions <= 0 or self.max_category_positions <= 0:
            raise ValueError("revenue position limits must be positive")
        if self.max_capital_deployed_usd > self.starting_balance_usd:
            raise ValueError("maximum deployed capital cannot exceed starting balance")
        if not 0 <= self.min_executable_edge <= 1:
            raise ValueError("executable edge threshold must be between zero and one")
        if min(
            self.daily_api_budget_usd,
            self.estimated_api_cost_per_call_usd,
            self.fee_bps,
            self.slippage_bps,
        ) < 0:
            raise ValueError("cost controls cannot be negative")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("maximum quote age must be positive")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
