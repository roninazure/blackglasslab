from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import json


@dataclass
class BaselineScore:
    p_yes_model: float
    confidence: float
    components: Dict[str, float]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_yes_price(market: Dict[str, Any]) -> float:
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")

    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)

    if not (isinstance(outcomes, list) and isinstance(prices, list)):
        return 0.5
    if len(outcomes) < 2 or len(prices) < 2:
        return 0.5

    for i, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() == "yes":
            return _clamp(_safe_float(prices[i], 0.5), 0.01, 0.99)

    return 0.5


def _hours_to_end(end_date: Any) -> Optional[float]:
    if not end_date:
        return None
    try:
        s = str(end_date).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        delta = dt - now
        return delta.total_seconds() / 3600.0
    except Exception:
        return None


def score_market(market: Dict[str, Any]) -> BaselineScore:
    """
    Deterministic Phase 1.9 baseline scorer.

    Philosophy:
    - Start from market probability
    - Only make small, explainable adjustments
    - Reward liquid/high-volume markets with higher confidence
    - Penalize ultra-extreme markets and very wide spreads
    """

    p_yes_market = _parse_yes_price(market)

    volume = _safe_float(market.get("volume"), 0.0)
    liquidity = _safe_float(market.get("liquidity"), 0.0)
    best_bid = _safe_float(market.get("bestBid"), p_yes_market)
    best_ask = _safe_float(market.get("bestAsk"), p_yes_market)
    last_trade = _safe_float(market.get("lastTradePrice"), p_yes_market)

    hours_to_end = _hours_to_end(market.get("endDate"))
    if hours_to_end is None:
        hours_to_end = 24.0 * 30.0

    # Normalize quality features conservatively
    liquidity_score = _clamp(liquidity / 100000.0, 0.0, 1.0)
    volume_score = _clamp(volume / 2500000.0, 0.0, 1.0)

    spread = abs(best_ask - best_bid) if best_ask and best_bid else 0.0
    spread_penalty = _clamp(spread / 0.10, 0.0, 1.0)  # 10-cent spread = bad

    # Long-dated markets deserve lower confidence than near-term,
    # but avoid extreme penalties.
    if hours_to_end <= 24:
        time_score = 1.0
    elif hours_to_end <= 24 * 7:
        time_score = 0.85
    elif hours_to_end <= 24 * 30:
        time_score = 0.70
    elif hours_to_end <= 24 * 90:
        time_score = 0.55
    else:
        time_score = 0.40

    # If market is extremely close to 0 or 1, be conservative.
    extremity = abs(p_yes_market - 0.5) * 2.0  # 0..1
    extremity_penalty = _clamp(extremity, 0.0, 1.0)

    # Tiny “last trade vs implied” nudge if present, capped hard.
    micro_signal = _clamp(last_trade - p_yes_market, -0.02, 0.02)

    # Baseline adjustment:
    # - small positive if liquid/active market
    # - small negative if spread is wide / market is too extreme
    quality_boost = (
        0.015 * liquidity_score
        + 0.010 * volume_score
        + 0.010 * time_score
        - 0.015 * spread_penalty
        - 0.010 * extremity_penalty
    )

    adjustment = _clamp(quality_boost + micro_signal, -0.03, 0.03)
    p_yes_model = _clamp(p_yes_market + adjustment, 0.01, 0.99)

    confidence = _clamp(
        0.25
        + 0.30 * liquidity_score
        + 0.20 * volume_score
        + 0.15 * time_score
        - 0.20 * spread_penalty,
        0.05,
        0.95,
    )

    return BaselineScore(
        p_yes_model=p_yes_model,
        confidence=confidence,
        components={
            "p_yes_market": p_yes_market,
            "volume": volume,
            "liquidity": liquidity,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "last_trade": last_trade,
            "spread": spread,
            "hours_to_end": hours_to_end,
            "liquidity_score": liquidity_score,
            "volume_score": volume_score,
            "time_score": time_score,
            "spread_penalty": spread_penalty,
            "extremity_penalty": extremity_penalty,
            "micro_signal": micro_signal,
            "quality_boost": quality_boost,
            "adjustment": adjustment,
        },
    )
