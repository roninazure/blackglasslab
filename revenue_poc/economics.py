from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


def _clamp(value: float, low: float = 0.001, high: float = 0.999) -> float:
    return min(high, max(low, float(value)))


@dataclass(frozen=True)
class ExecutionEconomics:
    model_probability: float
    market_probability: float
    executable_bid: float
    executable_ask: float
    spread: float
    depth_usd: float
    depth_source: str
    side: str
    entry_price: float
    fee_usd: float
    slippage_usd: float
    spread_cost_usd: float
    raw_edge: float
    executable_edge: float
    expected_value_usd: float
    capital_required_usd: float
    expected_holding_days: float | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_execution(
    *,
    model_probability: float,
    market_probability: float,
    stake_usd: float,
    best_bid: float | None = None,
    best_ask: float | None = None,
    spread: float | None = None,
    depth_usd: float | None = None,
    fee_rate: float | None = None,
    fee_bps: float = 0.0,
    slippage_bps: float = 0.0,
    expected_holding_days: float | None = None,
) -> ExecutionEconomics:
    model = _clamp(model_probability)
    market = _clamp(market_probability)
    quoted_spread = max(0.0, float(spread or 0.0))
    bid = float(best_bid) if best_bid is not None else market - quoted_spread / 2.0
    ask = float(best_ask) if best_ask is not None else market + quoted_spread / 2.0
    bid, ask = _clamp(bid), _clamp(ask)
    if ask < bid:
        raise ValueError("executable ask cannot be below executable bid")
    actual_spread = ask - bid
    side = "YES" if model >= market else "NO"
    model_win = model if side == "YES" else 1.0 - model
    midpoint_side = market if side == "YES" else 1.0 - market
    entry_price = ask if side == "YES" else 1.0 - bid
    entry_price = _clamp(entry_price)
    if fee_rate is not None:
        shares = stake_usd / entry_price
        fee = shares * max(0.0, fee_rate) * entry_price * (1.0 - entry_price)
    else:
        fee = stake_usd * max(0.0, fee_bps) / 10_000.0
    slippage = stake_usd * max(0.0, slippage_bps) / 10_000.0
    spread_cost = stake_usd * max(0.0, entry_price - midpoint_side) / entry_price
    raw_edge = abs(model - market)
    friction_probability = entry_price * (fee + slippage) / stake_usd
    executable_edge = model_win - entry_price - friction_probability
    expected_value = stake_usd * (model_win / entry_price - 1.0) - fee - slippage
    return ExecutionEconomics(
        model_probability=model,
        market_probability=market,
        executable_bid=bid,
        executable_ask=ask,
        spread=actual_spread,
        depth_usd=max(0.0, float(depth_usd or 0.0)),
        depth_source="liquidity_proxy" if depth_usd is not None else "unavailable",
        side=side,
        entry_price=entry_price,
        fee_usd=fee,
        slippage_usd=slippage,
        spread_cost_usd=spread_cost,
        raw_edge=raw_edge,
        executable_edge=executable_edge,
        expected_value_usd=expected_value,
        capital_required_usd=stake_usd + fee + slippage,
        expected_holding_days=expected_holding_days,
    )


def adaptive_threshold(*, spread: float, depth_usd: float, holding_days: float | None) -> float:
    """Evidence-capture counterfactual; fixed 2% remains the admission policy."""
    threshold = 0.02 + min(0.02, max(0.0, spread) / 2.0)
    if depth_usd < 5_000:
        threshold += 0.01
    elif depth_usd < 25_000:
        threshold += 0.005
    if holding_days is None:
        threshold += 0.005
    elif holding_days > 180:
        threshold += 0.005
    return round(min(0.08, threshold), 6)
