"""Revenue Velocity Shadow v1 analysis.

This module is deliberately read-only.  It ranks the latest valid discovery
universe using already-persisted executable evaluations and never participates
in production admission or position sizing.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from statistics import mean, median
from typing import Any

HORIZONS = ("FAST", "WEEKLY", "MONTHLY", "LONG")


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _utc(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def horizon_for_days(days: float | None) -> str | None:
    if days is None:
        return None
    if days <= 3:
        return "FAST"
    if days <= 14:
        return "WEEKLY"
    if days <= 45:
        return "MONTHLY"
    return "LONG"


def _days_to_resolution(metadata: dict[str, Any], timestamp_utc: str) -> float | None:
    temporal = metadata.get("temporal")
    if not isinstance(temporal, dict):
        temporal = metadata.get("temporal_context")
    if isinstance(temporal, dict):
        hours = _number(temporal.get("time_remaining_hours"))
        if hours is not None:
            return hours / 24.0
        end_date = temporal.get("market_resolution_date") or temporal.get("market_end_date")
    else:
        end_date = metadata.get("end_date")
    end = _utc(end_date)
    observed = _utc(timestamp_utc)
    if end is None or observed is None:
        return None
    return (end - observed).total_seconds() / 86400.0


def _resolution_date(metadata: dict[str, Any]) -> str | None:
    temporal = metadata.get("temporal")
    if not isinstance(temporal, dict):
        temporal = metadata.get("temporal_context")
    if isinstance(temporal, dict):
        return temporal.get("market_resolution_date") or temporal.get("market_end_date")
    return metadata.get("end_date")


def _liquidity(metadata: dict[str, Any]) -> float | None:
    return _number(
        metadata.get("liquidity")
        or metadata.get("depth_usd")
        or (metadata.get("scoring_components") or {}).get("raw", {}).get("liquidity")
    )


def _spread(metadata: dict[str, Any]) -> float | None:
    return _number(
        metadata.get("spread")
        or (metadata.get("scoring_components") or {}).get("raw", {}).get("spread")
    )


def _confidence(evaluation: sqlite3.Row | None) -> float | None:
    # Revenue evaluation persistence currently has no model-confidence field.
    # Do not manufacture one from probability, score, or edge.
    if evaluation is None:
        return None
    metadata = _json_object(evaluation["metadata"])
    return _number(metadata.get("confidence"))


def _liquidity_concern(depth: float | None, spread: float | None) -> list[str]:
    concerns: list[str] = []
    if depth is None:
        concerns.append("depth_unavailable")
    elif depth < 5_000:
        concerns.append("depth_below_5k")
    elif depth < 25_000:
        concerns.append("depth_below_25k")
    if spread is None:
        concerns.append("spread_unavailable")
    elif spread > 0.04:
        concerns.append("spread_above_4pct")
    elif spread > 0.02:
        concerns.append("spread_above_2pct")
    return concerns


def _latest_evaluations(
    conn: sqlite3.Connection, market_ids: set[str], cutoff_utc: str
) -> dict[str, sqlite3.Row]:
    if not market_ids:
        return {}
    rows = conn.execute(
        """
        SELECT e.*
        FROM revenue_poc_evaluations e
        JOIN (
          SELECT market_id,MAX(timestamp_utc) AS timestamp_utc
          FROM revenue_poc_evaluations
          WHERE timestamp_utc <= ?
          GROUP BY market_id
        ) latest ON latest.market_id=e.market_id AND latest.timestamp_utc=e.timestamp_utc
        WHERE e.market_id IN ({})
        ORDER BY e.id DESC
        """.format(",".join("?" for _ in market_ids)),
        (cutoff_utc, *sorted(market_ids)),
    ).fetchall()
    return {str(row["market_id"]): row for row in rows}


def _ranked_position(position: dict[str, Any], *, rank_key: str) -> tuple[Any, ...]:
    value = position.get(rank_key)
    return (value is None, -(float(value) if value is not None else 0.0), position["market_id"])


def velocity_shadow_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a pure read-only Revenue Velocity Shadow v1 report."""
    latest = conn.execute(
        """
        SELECT run_id,timestamp_utc,venue
        FROM revenue_poc_discovery_snapshots
        ORDER BY timestamp_utc DESC,id DESC LIMIT 1
        """
    ).fetchone()
    if latest is None:
        return {
            "schema_version": "revenue_velocity_shadow_v1",
            "status": "NO_DISCOVERY_DATA",
            "universe": {},
            "top_velocity": [],
        }

    discovery_rows = conn.execute(
        """
        SELECT * FROM revenue_poc_discovery_snapshots
        WHERE run_id=? AND venue=? AND status='VALID'
        ORDER BY id
        """,
        (latest["run_id"], latest["venue"]),
    ).fetchall()
    market_ids = {str(row["market_id"]) for row in discovery_rows}
    evaluations = _latest_evaluations(conn, market_ids, str(latest["timestamp_utc"]))
    existing = {
        str(row[0])
        for row in conn.execute(
            "SELECT market_id FROM revenue_poc_positions WHERE status='OPEN'"
        ).fetchall()
    }
    positions: list[dict[str, Any]] = []
    for row in discovery_rows:
        metadata = _json_object(row["metadata"])
        evaluation = evaluations.get(str(row["market_id"]))
        days = _days_to_resolution(metadata, str(latest["timestamp_utc"]))
        horizon = horizon_for_days(days)
        depth = _number(evaluation["depth_usd"]) if evaluation is not None else _liquidity(metadata)
        spread = _number(evaluation["spread"]) if evaluation is not None else _spread(metadata)
        edge = _number(evaluation["executable_edge"]) if evaluation is not None else None
        net_ev = _number(evaluation["expected_value_usd"]) if evaluation is not None else None
        capital = _number(evaluation["capital_required_usd"]) if evaluation is not None else None
        ev_per_day = (
            net_ev / (capital * days)
            if net_ev is not None and capital and days and capital > 0 and days > 0
            else None
        )
        raw = (metadata.get("scoring_components") or {}).get("raw", {})
        existing_exposure = bool(existing.intersection({str(row["market_id"])})) or bool(
            raw.get("existing_exposure")
        )
        positions.append(
            {
                "market_id": str(row["market_id"]),
                "question": (
                    evaluation["question"] if evaluation is not None else None
                ),
                "category": metadata.get("category") or (evaluation["category"] if evaluation else None),
                "horizon": horizon,
                "days_to_resolution": days,
                "resolution_date": _resolution_date(metadata),
                "executable_edge": edge,
                "modeled_net_ev_usd": net_ev,
                "capital_required_usd": capital,
                "ev_per_capital_day": ev_per_day,
                "liquidity_usd": depth,
                "spread": spread,
                "confidence": _confidence(evaluation),
                "confidence_status": "available" if _confidence(evaluation) is not None else "not_persisted",
                "existing_exposure": existing_exposure,
                "evaluation_status": (
                    evaluation["production_decision"] if evaluation is not None else "not_evaluated"
                ),
                "current_production_score": _number(row["deterministic_score"]),
                "dynamic_shortlist": bool(row["dynamic_shortlist"]),
                "current_ranking_exclusion_reason": (
                    None if row["dynamic_shortlist"] else "not_in_dynamic_shortlist"
                ),
                "liquidity_concerns": _liquidity_concern(depth, spread),
            }
        )

    current_sorted = sorted(positions, key=lambda item: _ranked_position(item, rank_key="current_production_score"))
    current_ranks = {item["market_id"]: index for index, item in enumerate(current_sorted, 1)}
    velocity_sorted = sorted(
        [item for item in positions if item["ev_per_capital_day"] is not None],
        key=lambda item: _ranked_position(item, rank_key="ev_per_capital_day"),
    )
    velocity_ranks = {item["market_id"]: index for index, item in enumerate(velocity_sorted, 1)}
    for item in positions:
        item["current_production_rank"] = current_ranks[item["market_id"]]
        item["velocity_rank"] = velocity_ranks.get(item["market_id"])
        item["rank_delta_current_minus_velocity"] = (
            item["current_production_rank"] - item["velocity_rank"]
            if item["velocity_rank"] is not None
            else None
        )

    by_horizon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in positions:
        by_horizon[item["horizon"] or "UNKNOWN"].append(item)
    horizon_counts = {}
    horizon_economics = {}
    horizon_market_stats = {}
    for horizon in (*HORIZONS, "UNKNOWN"):
        group = by_horizon.get(horizon, [])
        priced = [item for item in group if item["ev_per_capital_day"] is not None]
        horizon_counts[horizon] = {
            "valid_discovered": len(group),
            "evaluated": len(priced),
            "positive_net_ev": sum(1 for item in priced if item["modeled_net_ev_usd"] is not None and item["modeled_net_ev_usd"] > 0),
        }
        velocities = [item["ev_per_capital_day"] for item in priced]
        horizon_economics[horizon] = {
            "evaluated": len(velocities),
            "average_ev_per_capital_day": mean(velocities) if velocities else None,
            "median_ev_per_capital_day": median(velocities) if velocities else None,
        }
        depths = [item["liquidity_usd"] for item in group if item["liquidity_usd"] is not None]
        spreads = [item["spread"] for item in group if item["spread"] is not None]
        horizon_market_stats[horizon] = {
            "markets_with_liquidity": len(depths),
            "median_liquidity_usd": median(depths) if depths else None,
            "average_liquidity_usd": mean(depths) if depths else None,
            "markets_with_spread": len(spreads),
            "median_spread": median(spreads) if spreads else None,
            "average_spread": mean(spreads) if spreads else None,
        }

    short_excluded = [
        item
        for item in positions
        if item["horizon"] in {"FAST", "WEEKLY", "MONTHLY"}
        and not item["dynamic_shortlist"]
    ]
    api = conn.execute(
        """
        SELECT COALESCE(SUM(api_calls),0),COALESCE(SUM(estimated_cost_usd),0),
               COALESCE(SUM(unknown_cost_calls),0)
        FROM revenue_poc_api_daily
        """
    ).fetchone()
    top_velocity = [
        item
        for item in velocity_sorted
        if item["modeled_net_ev_usd"] is not None and item["modeled_net_ev_usd"] > 0
    ][:20]

    return {
        "schema_version": "revenue_velocity_shadow_v1",
        "status": "OK",
        "read_only": True,
        "source": {
            "run_id": latest["run_id"],
            "timestamp_utc": latest["timestamp_utc"],
            "venue": latest["venue"],
            "production_admission_unchanged": True,
        },
        "universe": {
            "valid_discovered_opportunities": len(positions),
            "evaluated_valid_opportunities": len(velocity_sorted),
            "unevaluated_valid_opportunities": len(positions) - len(velocity_sorted),
            "velocity_ranked_opportunities": len(velocity_sorted),
            "positive_net_ev_opportunities": sum(
                1
                for item in velocity_sorted
                if item["modeled_net_ev_usd"] is not None and item["modeled_net_ev_usd"] > 0
            ),
        },
        "candidate_counts_by_horizon": horizon_counts,
        "top_velocity": top_velocity,
        "rank_differences": [
            {
                "market_id": item["market_id"],
                "current_production_rank": item["current_production_rank"],
                "velocity_rank": item["velocity_rank"],
                "rank_delta_current_minus_velocity": item["rank_delta_current_minus_velocity"],
            }
            for item in velocity_sorted
        ],
        "short_horizon_excluded_by_current_ranking": short_excluded,
        "modeled_ev_per_capital_day_by_horizon": horizon_economics,
        "liquidity_spread_statistics_by_horizon": horizon_market_stats,
        "api_cost_impact": {
            "incremental_api_calls": 0,
            "incremental_estimated_cost_usd": 0.0,
            "existing_api_calls": int(api[0] or 0),
            "existing_estimated_cost_usd": float(api[1] or 0.0),
            "existing_unknown_cost_calls": int(api[2] or 0),
            "note": "Velocity ranking reuses persisted discovery and evaluation data; it makes no model or venue calls.",
        },
        "all_valid_opportunities": positions,
        "definitions": {
            "velocity": "modeled_net_ev_usd / (capital_required_usd * days_to_resolution)",
            "horizons": {"FAST": "0-3d", "WEEKLY": "4-14d", "MONTHLY": "15-45d", "LONG": ">45d"},
            "confidence": "Unavailable unless persisted by the existing evaluation data; no proxy is substituted.",
        },
    }


def velocity_coverage_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Measure executable-evaluation coverage across the latest valid universe."""
    latest = conn.execute(
        """
        SELECT run_id,timestamp_utc,venue
        FROM revenue_poc_discovery_snapshots
        ORDER BY timestamp_utc DESC,id DESC LIMIT 1
        """
    ).fetchone()
    if latest is None:
        return {
            "schema_version": "revenue_velocity_evaluation_coverage_v1",
            "status": "NO_DISCOVERY_DATA",
        }

    discovery_rows = conn.execute(
        """
        SELECT * FROM revenue_poc_discovery_snapshots
        WHERE run_id=? AND venue=? AND status='VALID'
        ORDER BY id
        """,
        (latest["run_id"], latest["venue"]),
    ).fetchall()
    market_ids = {str(row["market_id"]) for row in discovery_rows}
    evaluations = _latest_evaluations(conn, market_ids, str(latest["timestamp_utc"]))
    records: list[dict[str, Any]] = []
    for row in discovery_rows:
        metadata = _json_object(row["metadata"])
        days = _days_to_resolution(metadata, str(latest["timestamp_utc"]))
        horizon = horizon_for_days(days) or "UNKNOWN"
        evaluation = evaluations.get(str(row["market_id"]))
        records.append(
            {
                "market_id": str(row["market_id"]),
                "horizon": horizon,
                "days_to_resolution": days,
                "category": metadata.get("category") or (evaluation["category"] if evaluation else None),
                "liquidity_usd": _liquidity(metadata),
                "spread": _spread(metadata),
                "executable_edge": _number(evaluation["executable_edge"]) if evaluation else None,
                "modeled_net_ev_usd": _number(evaluation["expected_value_usd"]) if evaluation else None,
                "capital_required_usd": _number(evaluation["capital_required_usd"]) if evaluation else None,
                "dynamic_shortlist": bool(row["dynamic_shortlist"]),
                "current_production_score": _number(row["deterministic_score"]),
                "evaluated": evaluation is not None,
                "evaluation_status": evaluation["production_decision"] if evaluation else "not_evaluated",
            }
        )

    by_horizon: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        by_horizon[record["horizon"]].append(record)

    horizon_summary: dict[str, dict[str, Any]] = {}
    for horizon in (*HORIZONS, "UNKNOWN"):
        group = by_horizon.get(horizon, [])
        evaluated = [record for record in group if record["evaluated"]]
        edges = [record["executable_edge"] for record in evaluated if record["executable_edge"] is not None]
        velocities = [
            record["modeled_net_ev_usd"] / (record["capital_required_usd"] * record["days_to_resolution"])
            for record in evaluated
            if record["modeled_net_ev_usd"] is not None
            and record["capital_required_usd"]
            and record["capital_required_usd"] > 0
            and record["days_to_resolution"]
            and record["days_to_resolution"] > 0
        ]
        liquidities = [record["liquidity_usd"] for record in group if record["liquidity_usd"] is not None]
        spreads = [record["spread"] for record in group if record["spread"] is not None]
        shortlist_count = sum(1 for record in group if record["dynamic_shortlist"])
        horizon_summary[horizon] = {
            "valid_markets": len(group),
            "evaluated_markets": len(evaluated),
            "unevaluated_markets": len(group) - len(evaluated),
            "coverage_pct": (100.0 * len(evaluated) / len(group)) if group else None,
            "shortlisted_markets": shortlist_count,
            "shortlist_rate_pct": (100.0 * shortlist_count / len(group)) if group else None,
            "average_liquidity_usd": mean(liquidities) if liquidities else None,
            "average_spread": mean(spreads) if spreads else None,
            "average_executable_edge": mean(edges) if edges else None,
            "average_ev_per_capital_day": mean(velocities) if velocities else None,
            "median_ev_per_capital_day": median(velocities) if velocities else None,
        }

    excluded = [record for record in records if not record["evaluated"]]
    excluded_sorted = sorted(
        excluded,
        key=lambda record: _ranked_position(record, rank_key="current_production_score"),
    )
    velocity_candidates = [
        record
        for record in records
        if record["evaluated"]
        and record["modeled_net_ev_usd"] is not None
        and record["modeled_net_ev_usd"] > 0
        and record["capital_required_usd"]
        and record["capital_required_usd"] > 0
        and record["days_to_resolution"]
        and record["days_to_resolution"] > 0
    ]
    for record in velocity_candidates:
        record["ev_per_capital_day"] = record["modeled_net_ev_usd"] / (
            record["capital_required_usd"] * record["days_to_resolution"]
        )
    category_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in velocity_candidates:
        category_groups[str(record["category"] or "UNKNOWN")].append(record)
    category_velocity = []
    for category, group in category_groups.items():
        values = [record["ev_per_capital_day"] for record in group]
        best = max(group, key=lambda record: record["ev_per_capital_day"])
        category_velocity.append(
            {
                "category": category,
                "candidate_count": len(group),
                "average_ev_per_capital_day": mean(values),
                "median_ev_per_capital_day": median(values),
                "best_market_id": best["market_id"],
                "best_ev_per_capital_day": best["ev_per_capital_day"],
            }
        )
    category_velocity.sort(key=lambda item: item["average_ev_per_capital_day"], reverse=True)

    total_valid = len(records)
    total_shortlisted = sum(1 for record in records if record["dynamic_shortlist"])
    non_long = [horizon_summary[horizon]["shortlist_rate_pct"] for horizon in HORIZONS[:-1] if horizon_summary[horizon]["shortlist_rate_pct"] is not None]
    long_rate = horizon_summary["LONG"]["shortlist_rate_pct"]
    valid_share_long = (100.0 * horizon_summary["LONG"]["valid_markets"] / total_valid) if total_valid else None
    shortlist_share_long = (100.0 * horizon_summary["LONG"]["shortlisted_markets"] / total_shortlisted) if total_shortlisted else None
    long_favoring = bool(
        long_rate is not None
        and non_long
        and long_rate > max(non_long)
        and valid_share_long is not None
        and shortlist_share_long is not None
        and shortlist_share_long > valid_share_long
    )
    short_horizons = {"FAST", "WEEKLY"}
    short_evaluated = sum(horizon_summary[horizon]["evaluated_markets"] for horizon in short_horizons)
    short_positive = sum(
        1
        for record in velocity_candidates
        if record["horizon"] in short_horizons
    )
    if short_evaluated == 0:
        short_economic_conclusion = "not_estimable_no_FAST_or_WEEKLY_evaluations"
    elif short_positive:
        short_economic_conclusion = "potentially_justified_positive_short_horizon_candidates"
    else:
        short_economic_conclusion = "not_supported_by_current_positive_net_ev"

    api = conn.execute(
        """
        SELECT COALESCE(SUM(api_calls),0),COALESCE(SUM(estimated_cost_usd),0),
               COALESCE(SUM(unknown_cost_calls),0)
        FROM revenue_poc_api_daily
        """
    ).fetchone()
    return {
        "schema_version": "revenue_velocity_evaluation_coverage_v1",
        "status": "OK",
        "read_only": True,
        "source": {
            "run_id": latest["run_id"],
            "timestamp_utc": latest["timestamp_utc"],
            "venue": latest["venue"],
            "production_admission_unchanged": True,
        },
        "horizon_summary": horizon_summary,
        "excluded_before_evaluation": {
            "count": len(excluded),
            "by_horizon": {
                horizon: sum(1 for record in excluded if record["horizon"] == horizon)
                for horizon in (*HORIZONS, "UNKNOWN")
            },
            "by_category": dict(sorted(
                ((category, sum(1 for record in excluded if record["category"] == category))
                 for category in {str(record["category"] or "UNKNOWN") for record in excluded}),
                key=lambda item: item[1], reverse=True,
            )),
            "top_by_current_production_rank": excluded_sorted[:20],
        },
        "category_velocity_candidates": category_velocity,
        "shortlist_bias": {
            "long_duration_favoring": long_favoring,
            "long_valid_share_pct": valid_share_long,
            "long_shortlist_share_pct": shortlist_share_long,
            "long_shortlist_rate_pct": long_rate,
            "interpretation": (
                "LONG is selected at a higher rate and is overrepresented in the shortlist."
                if long_favoring
                else "No systematic LONG-duration shortlist bias detected by selection rate and share."
            ),
        },
        "short_horizon_economic_justification": {
            "FAST_WEEKLY_evaluated": short_evaluated,
            "FAST_WEEKLY_positive_net_ev_candidates": short_positive,
            "conclusion": short_economic_conclusion,
            "recommendation": (
                "Increase shadow-only FAST/WEEKLY coverage before changing production ranking."
                if short_evaluated == 0
                else "Use the observed short-horizon candidate economics for a controlled shadow comparison."
            ),
        },
        "api_cost_impact": {
            "incremental_api_calls_for_report": 0,
            "incremental_estimated_cost_usd": 0.0,
            "existing_api_calls": int(api[0] or 0),
            "existing_estimated_cost_usd": float(api[1] or 0.0),
            "existing_unknown_cost_calls": int(api[2] or 0),
        },
        "definitions": {
            "evaluated": "A valid discovery market with a persisted executable Revenue evaluation at or before the latest discovery snapshot.",
            "coverage_pct": "evaluated_markets / valid_markets * 100",
            "ev_per_capital_day": "modeled_net_ev_usd / (capital_required_usd * days_to_resolution)",
            "horizons": {"FAST": "0-3d", "WEEKLY": "4-14d", "MONTHLY": "15-45d", "LONG": ">45d"},
        },
    }
