from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import json


@dataclass
class BaselineScore:
    p_yes_market: float
    p_yes_model: float
    confidence: float
    spread: float
    pricing_source: str
    reject_reason: Optional[str]
    components: Dict[str, float]


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def _parse_yes_price_from_outcomes(market: Dict[str, Any]) -> Optional[float]:
    outcomes = market.get("outcomes")
    prices = market.get("outcomePrices")

    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    if isinstance(prices, str):
        prices = json.loads(prices)

    if not (isinstance(outcomes, list) and isinstance(prices, list)):
        return None
    if len(outcomes) < 2 or len(prices) < 2:
        return None

    for i, outcome in enumerate(outcomes):
        if str(outcome).strip().lower() == "yes":
            return _clamp(_safe_float(prices[i], 0.5), 0.01, 0.99)
    return None


def _hours_to_end(end_date: Any) -> Optional[float]:
    if not end_date:
        return None
    try:
        s = str(end_date).replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        now = datetime.now(timezone.utc)
        return (dt - now).total_seconds() / 3600.0
    except Exception:
        return None


def market_yes_price(market: Dict[str, Any]) -> tuple[float, float, str]:
    """
    Returns:
      (p_yes_market, spread, pricing_source)

    Pricing priority:
      1) midpoint(bestBid, bestAsk)
      2) lastTradePrice
      3) outcomePrices[Yes]
      4) fallback 0.50
    """
    best_bid = _safe_float(market.get("bestBid"), 0.0)
    best_ask = _safe_float(market.get("bestAsk"), 0.0)
    last_trade = _safe_float(market.get("lastTradePrice"), 0.0)
    yes_outcome = _parse_yes_price_from_outcomes(market)

    if best_bid > 0 and best_ask > 0 and best_ask >= best_bid:
        mid = (best_bid + best_ask) / 2.0
        spread = max(0.0, best_ask - best_bid)
        return (_clamp(mid, 0.01, 0.99), spread, "mid")

    if last_trade > 0:
        return (_clamp(last_trade, 0.01, 0.99), 0.0, "last_trade")

    if yes_outcome is not None:
        return (_clamp(yes_outcome, 0.01, 0.99), 0.0, "outcome_yes")

    return (0.50, 0.0, "fallback")


def score_market(market: Dict[str, Any]) -> BaselineScore:
    """
    Phase 2.0A deterministic baseline:
    - execution-aware pricing (mid-price preferred)
    - market quality gates
    - conservative model adjustment
    """
    p_yes_market, spread, pricing_source = market_yes_price(market)

    active = bool(market.get("active"))
    closed = bool(market.get("closed"))
    volume = _safe_float(market.get("volume"), 0.0)
    liquidity = _safe_float(market.get("liquidity"), 0.0)
    best_bid = _safe_float(market.get("bestBid"), p_yes_market)
    best_ask = _safe_float(market.get("bestAsk"), p_yes_market)
    last_trade = _safe_float(market.get("lastTradePrice"), p_yes_market)
    hours_to_end = _hours_to_end(market.get("endDate"))
    if hours_to_end is None:
        hours_to_end = 24.0 * 30.0

    # Hard market-quality gates
    if not active:
        return BaselineScore(
            p_yes_market=p_yes_market,
            p_yes_model=p_yes_market,
            confidence=0.0,
            spread=spread,
            pricing_source=pricing_source,
            reject_reason="inactive_market",
            components={},
        )
    if closed:
        return BaselineScore(
            p_yes_market=p_yes_market,
            p_yes_model=p_yes_market,
            confidence=0.0,
            spread=spread,
            pricing_source=pricing_source,
            reject_reason="closed_market",
            components={},
        )
    if liquidity < 10000:
        return BaselineScore(
            p_yes_market=p_yes_market,
            p_yes_model=p_yes_market,
            confidence=0.0,
            spread=spread,
            pricing_source=pricing_source,
            reject_reason="low_liquidity",
            components={},
        )
    if volume < 50000:
        return BaselineScore(
            p_yes_market=p_yes_market,
            p_yes_model=p_yes_market,
            confidence=0.0,
            spread=spread,
            pricing_source=pricing_source,
            reject_reason="low_volume",
            components={},
        )
    if spread > 0.03:
        return BaselineScore(
            p_yes_market=p_yes_market,
            p_yes_model=p_yes_market,
            confidence=0.0,
            spread=spread,
            pricing_source=pricing_source,
            reject_reason="wide_spread",
            components={},
        )

    # Normalized features
    liquidity_score = _clamp(liquidity / 100000.0, 0.0, 1.0)
    volume_score = _clamp(volume / 2500000.0, 0.0, 1.0)
    spread_penalty = _clamp(spread / 0.03, 0.0, 1.0)

    if hours_to_end <= 24:
        time_score = 1.00
    elif hours_to_end <= 24 * 7:
        time_score = 0.85
    elif hours_to_end <= 24 * 30:
        time_score = 0.70
    elif hours_to_end <= 24 * 90:
        time_score = 0.55
    else:
        time_score = 0.40

    # --- Directional signals ---

    # Momentum: last trade vs current mid — capped at ±2.5%
    micro_signal = _clamp(last_trade - p_yes_market, -0.025, 0.025)

    # Bid-ask pressure: if last trade diverges from the mid-point of the current book
    bid_ask_pressure = 0.0
    if best_bid > 0 and best_ask > 0:
        book_mid = (best_bid + best_ask) / 2.0
        bid_ask_pressure = _clamp(last_trade - book_mid, -0.010, 0.010)

    # Tail mean-reversion: REMOVED (Phase 3).
    # This heuristic was pulling extreme markets (1%) toward centre (+1.5%),
    # generating phantom Tier-A edges on near-certain markets. The market price
    # at extremes is usually correct — LLM reasoning handles the exceptions.
    tail_reversion = 0.0

    # Quality nudge: minimal directional contribution; wide spread keeps model neutral
    quality_nudge = (
        0.004 * liquidity_score
        + 0.003 * volume_score
        - 0.006 * spread_penalty
    )

    adjustment = _clamp(micro_signal + bid_ask_pressure + tail_reversion + quality_nudge, -0.05, 0.05)
    p_yes_model = _clamp(p_yes_market + adjustment, 0.01, 0.99)

    confidence = _clamp(
        0.25
        + 0.28 * liquidity_score
        + 0.18 * volume_score
        + 0.12 * time_score
        - 0.18 * spread_penalty,
        0.05,
        0.95,
    )

    return BaselineScore(
        p_yes_market=p_yes_market,
        p_yes_model=p_yes_model,
        confidence=confidence,
        spread=spread,
        pricing_source=pricing_source,
        reject_reason=None,
        components={
            "volume": volume,
            "liquidity": liquidity,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "last_trade": last_trade,
            "hours_to_end": hours_to_end,
            "liquidity_score": liquidity_score,
            "volume_score": volume_score,
            "time_score": time_score,
            "spread_penalty": spread_penalty,
            "micro_signal": micro_signal,
            "bid_ask_pressure": bid_ask_pressure,
            "tail_reversion": tail_reversion,
            "quality_nudge": quality_nudge,
            "adjustment": adjustment,
        },
    )
