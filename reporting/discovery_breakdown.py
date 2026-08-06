"""Read-only discovery funnel reporting.

This module consumes the persisted inference report and, optionally, the
read-only Revenue database.  It deliberately does not call a venue adapter or
write reports/databases.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from collections.abc import Mapping
from typing import Any

EXAMPLE_LIMIT = 3
_PRE_SCORE_REASONS = {
    "inactive": "inactive",
    "closed": "closed",
    "duplicate_or_missing_id": "duplicate_or_missing_id",
    "malformed": "malformed",
}
_QUALITY_REASONS = (
    "low_institutional_quality",
    "weak_resolution_quality",
    "low_liquidity",
    "excessive_spread",
    "insufficient_depth",
    "stale_or_unavailable",
    "temporal_inconsistency",
    "policy_exclusion",
    "other",
)


def _pct(value: float, denominator: float) -> float:
    return round((float(value) / float(denominator) * 100.0), 2) if denominator else 0.0


def _rows(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    shadow = report.get("discovery_shadow")
    if isinstance(shadow, Mapping) and isinstance(shadow.get("rows"), list):
        return [row for row in shadow["rows"] if isinstance(row, dict)]
    return []


def _examples(rows: list[dict[str, Any]], reason: str, limit: int) -> list[str]:
    result: list[str] = []
    for row in rows:
        if str(row.get("reason") or row.get("features", {}).get("policy_reason")) != reason:
            continue
        market_id = str(row.get("market_id") or "").strip()
        if market_id and market_id not in result:
            result.append(market_id)
        if len(result) >= limit:
            break
    return result


def _discovery(report: Mapping[str, Any]) -> Mapping[str, Any]:
    optimization = report.get("optimization")
    if isinstance(optimization, Mapping) and isinstance(optimization.get("discovery"), Mapping):
        return optimization["discovery"]
    shadow = report.get("discovery_shadow")
    if isinstance(shadow, Mapping):
        scan = shadow.get("scan")
        if isinstance(scan, Mapping):
            return {**scan, "rejected_by_reason": shadow.get("rejected_by_reason", {})}
    return {}


def _latest_revenue(conn: Any, run_id: str) -> tuple[set[str], dict[str, str], set[str]]:
    if conn is None or not run_id:
        return set(), {}, set()
    try:
        evaluations = conn.execute(
            "SELECT market_id, production_decision, production_rejection_reason "
            "FROM revenue_poc_evaluations WHERE run_id=?",
            (run_id,),
        ).fetchall()
    except (AttributeError, sqlite3.Error):
        return set(), {}, set()
    evaluated = {str(row[0]) for row in evaluations}
    decisions = {str(row[0]): str(row[2] or row[1] or "other") for row in evaluations}
    try:
        admitted = {
            str(row[0])
            for row in conn.execute(
                "SELECT e.market_id FROM revenue_poc_decisions d "
                "JOIN revenue_poc_evaluations e ON e.id=d.evaluation_id "
                "WHERE e.run_id=? AND d.decision='ADMIT'",
                (run_id,),
            ).fetchall()
        }
    except (AttributeError, sqlite3.Error):
        admitted = set()
    return evaluated, decisions, admitted


def build_discovery_breakdown(
    report: Mapping[str, Any], *, conn: Any = None, example_limit: int = EXAMPLE_LIMIT
) -> dict[str, Any]:
    """Build a JSON-serializable funnel breakdown from existing state only."""
    discovery = _discovery(report)
    reasons_raw = discovery.get("rejected_by_reason", {})
    reasons = Counter(
        {str(key): int(value or 0) for key, value in reasons_raw.items()}
        if isinstance(reasons_raw, Mapping)
        else {}
    )
    total = int(discovery.get("total_discovered", 0) or 0)
    inactive = reasons["inactive"]
    closed = reasons["closed"]
    duplicate = reasons["duplicate_or_missing_id"]
    malformed = reasons["malformed"]
    scored = max(0, total - inactive - closed - duplicate - malformed)
    valid = int(discovery.get("valid_contracts", 0) or 0)
    shortlisted = int(discovery.get("shortlisted", discovery.get("shortlisted_contracts", 0)) or 0)
    outside = int(
        discovery.get(
            "outside_fixed_watchlist",
            discovery.get("opportunities_outside_fixed_watchlist", 0),
        )
        or 0
    )
    rows = _rows(report)
    run_id = str(report.get("run_id") or "")
    evaluated_ids, revenue_reasons, admitted_ids = _latest_revenue(conn, run_id)
    shadow = report.get("discovery_shadow")
    selected = [
        row
        for row in (shadow.get("selected") if isinstance(shadow, Mapping) and isinstance(shadow.get("selected"), list) else [])
        if isinstance(row, dict)
    ]
    selected_ids = {str(row.get("market_id")) for row in selected}
    shortlist_not_evaluated = [
        str(row.get("market_id"))
        for row in selected
        if str(row.get("market_id")) not in evaluated_ids
    ]
    gap = Counter()
    gap_examples: dict[str, list[str]] = {}
    pipeline_reasons = {
        str(row.get("market_id")): str(row.get("reason") or "")
        for row in (report.get("markets") if isinstance(report.get("markets"), list) else [])
        if isinstance(row, dict)
    }
    for row in selected:
        market_id = str(row.get("market_id") or "")
        if market_id in evaluated_ids:
            continue
        if not row.get("fixed_watchlist"):
            reason = "outside_fixed_watchlist"
        elif pipeline_reasons.get(market_id) in {
            "existing_open_or_pending_position",
            "one_position_per_contract",
        } or revenue_reasons.get(market_id) in {
            "existing_open_or_pending_position",
            "one_position_per_contract",
        }:
            reason = "duplicate_position"
        else:
            reason = "other"
        gap[reason] += 1
        gap_examples.setdefault(reason, [])
        if len(gap_examples[reason]) < example_limit:
            gap_examples[reason].append(market_id)

    banned_count = reasons["banned_market_class"]
    banned_classes = {
        "generic_banned_market_class": {
            "count": banned_count,
            "percentage_of_scored": _pct(banned_count, scored),
            "examples": _examples(rows, "banned_market_class", example_limit),
            "subtype_metadata_available": False,
        }
    }
    quality_counts = {
        "low_institutional_quality": reasons["low_institutional_quality"],
        "weak_resolution_quality": reasons["weak_resolution_quality"],
        "low_liquidity": reasons["low_liquidity"],
        "excessive_spread": reasons["excessive_spread"] + reasons["wide_spread"],
        "insufficient_depth": reasons["insufficient_depth"],
        "stale_or_unavailable": reasons["stale_or_unavailable"] + reasons["stale"],
        "temporal_inconsistency": reasons["temporal_inconsistency"],
        "policy_exclusion": sum(
            reasons[key]
            for key in ("banned_market_class", "low_institutional_quality", "weak_resolution_quality")
        ),
    }
    post_score_reasons = sum(
        count for reason, count in reasons.items() if reason not in _PRE_SCORE_REASONS
    )
    named_quality = reasons["banned_market_class"] + sum(
        count
        for key, count in quality_counts.items()
        if key != "policy_exclusion"
    )
    quality_counts["other"] = max(0, post_score_reasons - named_quality)
    quality_examples = {
        "policy_exclusion": (
            _examples(rows, "banned_market_class", example_limit)
            + _examples(rows, "low_institutional_quality", example_limit)
            + _examples(rows, "weak_resolution_quality", example_limit)
        )[:example_limit]
    }
    quality = {
        key: {
            "count": count,
            "percentage_of_scored": _pct(count, scored),
            "examples": quality_examples.get(key, _examples(rows, key, example_limit)),
        }
        for key, count in quality_counts.items()
    }

    revenue_evaluated = len(evaluated_ids)
    revenue_admitted = len(admitted_ids)
    revenue_rejected = max(0, revenue_evaluated - revenue_admitted)
    stages = [
        ("scored", scored, total),
        ("valid_after_policy", valid, scored),
        ("shortlisted", shortlisted, valid),
        ("shortlisted_outside_fixed_watchlist", outside, shortlisted),
        ("eligible_for_revenue_evaluation", revenue_evaluated, shortlisted),
        ("actually_evaluated", revenue_evaluated, revenue_evaluated),
        ("admitted", revenue_admitted, revenue_evaluated),
        ("rejected", revenue_rejected, revenue_evaluated),
    ]
    funnel = [
        {
            "stage": name,
            "count": count,
            "percentage_of_previous": _pct(count, previous),
            "percentage_of_total_discovered": _pct(count, total),
        }
        for name, count, previous in stages
    ]
    return {
        "cycle": {"run_id": run_id, "timestamp_utc": report.get("ts_utc")},
        "read_only": True,
        "discovery_source": {
            "events_fetched": 500,
            "events_fetched_observed": False,
            "configured_event_limit": 500,
            "total_market_records_expanded": total,
            "total_active": max(0, total - inactive),
            "total_inactive": inactive,
            "total_closed": closed,
            "duplicates": duplicate,
            "malformed_or_missing_ids": malformed,
            "limitation": "The persisted snapshot records the configured event request limit, not the raw event response count.",
        },
        "banned_market_classes": banned_classes,
        "banned_market_class_limitation": "Existing discovery data records only generic banned_market_class; no supported subtype metadata was persisted.",
        "quality_exclusions": quality,
        "survivor_funnel": funnel,
        "shortlist_to_revenue_gap": {
            "shortlist_count": shortlisted,
            "shortlist_evaluated_count": len(selected_ids & evaluated_ids),
            "shortlist_not_evaluated_count": len(shortlist_not_evaluated),
            "groups": {
                key: {"count": count, "examples": gap_examples.get(key, [])}
                for key, count in gap.items()
            },
            "not_evaluated_examples": shortlist_not_evaluated[:example_limit],
            "additional_revenue_evaluations_outside_shortlist": len(evaluated_ids - selected_ids),
            "additional_revenue_evaluation_examples": sorted(evaluated_ids - selected_ids)[:example_limit],
        },
    }


def render_discovery_breakdown(data: Mapping[str, Any]) -> str:
    """Render a compact operator-facing text report."""
    source = data["discovery_source"]
    lines = [
        f"DISCOVERY BREAKDOWN cycle={data['cycle'].get('run_id')}",
        (
            f"SOURCE events_fetched={source['events_fetched']} (configured limit; raw response count unavailable) "
            f"expanded={source['total_market_records_expanded']} active={source['total_active']} "
            f"inactive={source['total_inactive']} closed={source['total_closed']} "
            f"duplicates={source['duplicates']} malformed_or_missing_ids={source['malformed_or_missing_ids']}"
        ),
        "BANNED CLASSES",
    ]
    for name, item in data["banned_market_classes"].items():
        lines.append(f"  {name} count={item['count']} ({item['percentage_of_scored']}% scored) examples={','.join(item['examples']) or 'none'}")
    lines.append("QUALITY EXCLUSIONS")
    for name, item in data["quality_exclusions"].items():
        lines.append(f"  {name} count={item['count']} ({item['percentage_of_scored']}% scored) examples={','.join(item['examples']) or 'none'}")
    lines.append("SURVIVOR FUNNEL")
    for item in data["survivor_funnel"]:
        lines.append(f"  {item['stage']} count={item['count']} prev={item['percentage_of_previous']}% total={item['percentage_of_total_discovered']}%")
    gap = data["shortlist_to_revenue_gap"]
    lines.append(f"SHORTLIST_TO_REVENUE shortlist_not_evaluated={gap['shortlist_not_evaluated_count']} additional_outside_shortlist={gap['additional_revenue_evaluations_outside_shortlist']}")
    for name, item in gap["groups"].items():
        lines.append(f"  {name} count={item['count']} examples={','.join(item['examples']) or 'none'}")
    return "\n".join(lines)
