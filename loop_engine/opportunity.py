from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional


_NOVELTY_PATTERNS = (
    r"\bbefore gta (?:vi|6)\b",
    r"\baliens?\b",
    r"\balbum\b",
    r"\bjesus\b",
    r"\brapture\b",
    r"\bsecond coming\b",
    r"\bmeme\b",
)

_CATEGORY_WEIGHTS = {
    "macro/fed": 10.0,
    "macro/econ": 9.5,
    "politics": 9.0,
    "legal": 8.5,
    "geopolitics": 8.0,
    "crypto": 7.5,
    "sports": 5.0,
    "novelty/other": 2.0,
}


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))


def _log_score(value: float, floor: float, target: float, points: float) -> float:
    if value <= floor:
        return 0.0
    numerator = math.log10(value / floor)
    denominator = math.log10(target / floor)
    return points * _clamp(numerator / denominator, 0.0, 1.0)


def _grade(score: float) -> str:
    if score >= 85:
        return "A"
    if score >= 70:
        return "B"
    if score >= 55:
        return "C"
    if score >= 40:
        return "D"
    return "F"


def is_novelty_market(question: str) -> bool:
    text = (question or "").lower()
    return any(re.search(pattern, text) for pattern in _NOVELTY_PATTERNS)


@dataclass(frozen=True)
class OpportunityScore:
    opportunity_score: float
    opportunity_grade: str
    scoring_components: dict[str, Any]
    eligible_for_llm: bool
    skip_reason: Optional[str]


def score_opportunity(
    market: Mapping[str, Any],
    *,
    category: str,
    p_yes_market: float,
    spread: float,
    temporal_context: Mapping[str, Any],
    min_score_for_llm: float,
    existing_exposure: bool = False,
    duplicate_position: bool = False,
    recent_cooldown: bool = False,
    quality_reject_reason: Optional[str] = None,
) -> OpportunityScore:
    liquidity = max(0.0, _number(market.get("liquidity")))
    volume = max(0.0, _number(market.get("volume")))
    spread = max(0.0, float(spread))
    probability = _clamp(float(p_yes_market), 0.0, 1.0)
    hours_remaining = temporal_context.get("time_remaining_hours")
    question = str(market.get("question") or "")

    liquidity_score = _log_score(liquidity, 1_000.0, 100_000.0, 18.0)
    volume_score = _log_score(volume, 10_000.0, 2_500_000.0, 14.0)
    spread_score = 14.0 * (1.0 - _clamp(spread / 0.03, 0.0, 1.0))
    probability_score = 12.0 * _clamp(
        1.0 - abs(probability - 0.5) / 0.47, 0.0, 1.0
    )

    if hours_remaining is None:
        resolution_score = 3.0
    else:
        hours = float(hours_remaining)
        if hours <= 0:
            resolution_score = 0.0
        elif hours <= 24 * 30:
            resolution_score = 10.0
        elif hours <= 24 * 90:
            resolution_score = 8.0
        elif hours <= 24 * 365:
            resolution_score = 6.0
        else:
            resolution_score = 3.0

    category_score = _CATEGORY_WEIGHTS.get(category, 2.0)
    temporal_score = 8.0 if temporal_context.get("market_end_date") else 2.0
    novelty = is_novelty_market(question) or category == "novelty/other"
    novelty_quality = -14.0 if novelty else 6.0
    exposure_quality = 0.0 if existing_exposure else 8.0

    components: dict[str, Any] = {
        "liquidity_quality": round(liquidity_score, 2),
        "volume_quality": round(volume_score, 2),
        "spread_quality": round(spread_score, 2),
        "probability_band_quality": round(probability_score, 2),
        "resolution_horizon_quality": round(resolution_score, 2),
        "category_quality": round(category_score, 2),
        "temporal_metadata_quality": round(temporal_score, 2),
        "novelty_quality": round(novelty_quality, 2),
        "exposure_quality": round(exposure_quality, 2),
        "raw": {
            "liquidity": liquidity,
            "volume": volume,
            "spread": spread,
            "p_yes_market": probability,
            "time_remaining_hours": hours_remaining,
            "category": category,
            "novelty_detected": novelty,
            "existing_exposure": existing_exposure,
            "duplicate_position": duplicate_position,
            "recent_cooldown": recent_cooldown,
            "quality_reject_reason": quality_reject_reason,
        },
    }
    score = round(
        liquidity_score
        + volume_score
        + spread_score
        + probability_score
        + resolution_score
        + category_score
        + temporal_score
        + novelty_quality
        + exposure_quality,
        2,
    )

    skip_reason: Optional[str] = None
    if duplicate_position:
        skip_reason = "duplicate_position"
    elif recent_cooldown:
        skip_reason = "recent_cooldown"
    elif quality_reject_reason:
        skip_reason = "weak_market_quality"
    elif score < min_score_for_llm:
        skip_reason = "low_opportunity_score"

    return OpportunityScore(
        opportunity_score=score,
        opportunity_grade=_grade(score),
        scoring_components=components,
        eligible_for_llm=skip_reason is None,
        skip_reason=skip_reason,
    )
