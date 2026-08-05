"""Cheap deterministic market discovery and ranking.

The module accepts already-fetched market dictionaries so callers can use a
fixture, an adapter page, or an offline copied snapshot.  It never calls a
model and never writes the production watchlist.
"""
from __future__ import annotations

import math
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from loop_engine.opportunity import score_opportunity
from loop_engine.prompts import classify_market
from market_universe.policy import InstitutionalUniverseConfig, evaluate_market
from context.temporal import build_temporal_context


def _num(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _market_id(market: Mapping[str, Any]) -> str:
    return str(market.get("market_id") or market.get("slug") or market.get("id") or "").strip()


def deterministic_score(market: Mapping[str, Any], *, now: datetime | None = None) -> tuple[float, dict[str, Any]]:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    question = str(market.get("question") or "")
    category = str(market.get("category") or classify_market(question))
    end = market.get("endDate") or market.get("end_date")
    temporal = build_temporal_context(dict(market), question=question, slug=_market_id(market), now=now)
    p = market.get("p_yes_market", market.get("lastTradePrice", market.get("outcomePrices", 0.5)))
    try:
        p = float(p)
    except (TypeError, ValueError):
        p = 0.5
    spread = abs(_num(market.get("bestAsk")) - _num(market.get("bestBid")))
    if not spread:
        spread = _num(market.get("spread"))
    liquidity = _num(market.get("liquidity"))
    volume = _num(market.get("volume"))
    depth = _num(market.get("depth_usd", market.get("depth")))
    freshness = _num(market.get("freshness_hours"))
    movement = abs(_num(market.get("price_change_24h", market.get("priceChange24h"))))
    quality = evaluate_market(market, config=InstitutionalUniverseConfig.from_env(), now=now)
    opportunity = score_opportunity(
        market,
        category=category,
        p_yes_market=p,
        spread=spread,
        temporal_context=temporal,
        min_score_for_llm=0.0,
        quality_reject_reason=None,
    )
    # A cheap ranking feature, separate from the strict admission lane.
    score = (
        opportunity.opportunity_score
        + min(12.0, math.log10(max(liquidity, 1.0)) * 1.5)
        + min(10.0, math.log10(max(volume, 1.0)) * 1.2)
        + min(10.0, math.log10(max(depth, 1.0)) * 1.2)
        + min(6.0, movement * 100.0)
        - min(8.0, freshness / 6.0)
    )
    return round(score, 4), {
        "liquidity": liquidity,
        "volume": volume,
        "depth_usd": depth,
        "spread": spread,
        "price_movement_24h": movement,
        "freshness_hours": freshness,
        "category": category,
        "policy_allowed": quality.policy_allowed,
        "policy_reason": quality.policy_reason,
        "temporal": temporal,
        "opportunity_score": opportunity.opportunity_score,
        "end_date": end,
    }


def discover_markets(
    markets: Iterable[Mapping[str, Any]],
    *,
    fixed_watchlist: Iterable[str] = (),
    shortlist_size: int = 25,
    now: datetime | None = None,
) -> dict[str, Any]:
    fixed = {str(item).strip() for item in fixed_watchlist if str(item).strip()}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    for raw in markets:
        market = dict(raw)
        market_id = _market_id(market)
        if not market_id or market_id in seen:
            reasons["duplicate_or_missing_id"] += 1
            continue
        seen.add(market_id)
        if not market.get("active", True):
            reasons["inactive"] += 1
            continue
        if market.get("closed", False):
            reasons["closed"] += 1
            continue
        try:
            score, features = deterministic_score(market, now=now)
        except Exception as exc:
            reasons["malformed"] += 1
            rows.append({"market_id": market_id, "status": "REJECTED", "reason": "malformed", "error": str(exc)[:200]})
            continue
        if not features["policy_allowed"]:
            reasons[str(features["policy_reason"] or "unsupported")] += 1
            rows.append({"market_id": market_id, "status": "REJECTED", "reason": features["policy_reason"], "score": score, "features": features})
            continue
        rows.append({
            "market_id": market_id,
            "status": "VALID",
            "fixed_watchlist": market_id in fixed,
            "score": score,
            "features": features,
            "market": market,
        })
    valid = sorted((row for row in rows if row["status"] == "VALID"), key=lambda row: row["score"], reverse=True)
    shortlist = valid[: max(0, int(shortlist_size))]
    shortlisted = {row["market_id"] for row in shortlist}
    for row in rows:
        if row["market_id"] in shortlisted:
            row["dynamic_shortlist"] = True
    return {
        "scan": {
            "total_discovered": len(seen),
            "valid_contracts": len(valid),
            "shortlisted_contracts": len(shortlist),
            "opportunities_outside_fixed_watchlist": sum(
                1 for row in shortlist if not row.get("fixed_watchlist")
            ),
        },
        "rejected_by_reason": dict(reasons),
        "selected": shortlist,
        "rows": rows,
    }
