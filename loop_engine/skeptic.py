from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


RISKY_CATEGORIES = {"legal", "geopolitics", "sports", "novelty/other"}


@dataclass(frozen=True)
class SkepticReview:
    action: str
    reason: str
    rationale: str
    temporal_valid: bool
    stale_facts: bool
    malformed_or_novelty: bool
    edge_real: bool


def should_request_skeptic(
    *,
    edge_abs: float,
    confidence: float,
    category: str,
    temporal_context: Mapping[str, Any],
    candidate_threshold: float,
    near_threshold_ratio: float,
    high_confidence: float,
) -> tuple[bool, str]:
    if edge_abs >= candidate_threshold:
        return True, "candidate_edge"
    if edge_abs >= candidate_threshold * near_threshold_ratio:
        return True, "near_threshold_edge"
    if confidence >= high_confidence and category in RISKY_CATEGORIES:
        return True, "high_confidence_risky_category"
    if category == "novelty/other" and (
        temporal_context.get("event_status") == "UNKNOWN"
        or not temporal_context.get("market_end_date")
    ):
        return True, "ambiguous_novelty_temporal_context"
    return False, "not_needed"


def normalize_skeptic_review(payload: Mapping[str, Any]) -> SkepticReview:
    action = str(payload.get("action") or "REJECT").strip().upper()
    if action not in {"ALLOW", "DOWNGRADE", "REJECT"}:
        action = "REJECT"
    return SkepticReview(
        action=action,
        reason=str(payload.get("reason") or "unspecified")[:120],
        rationale=str(payload.get("rationale") or "")[:500],
        temporal_valid=bool(payload.get("temporal_valid", False)),
        stale_facts=bool(payload.get("stale_facts", True)),
        malformed_or_novelty=bool(payload.get("malformed_or_novelty", False)),
        edge_real=bool(payload.get("edge_real", False)),
    )
