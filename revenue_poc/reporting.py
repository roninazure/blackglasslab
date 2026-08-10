from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def portfolio_dashboard(conn: sqlite3.Connection) -> dict[str, Any]:
    account = conn.execute(
        "SELECT starting_balance_usd,daily_api_budget_usd FROM revenue_poc_accounts WHERE id=1"
    ).fetchone()
    if account is None:
        raise RuntimeError("Revenue POC is not initialized")
    positions = conn.execute(
        """
        SELECT id,status,size_usd,fee_usd,slippage_usd,spread_cost_usd,
               expected_value_usd,realized_pnl_usd,gross_realized_pnl_usd,
               realized_fee_usd,realized_slippage_usd,side,resolved_outcome
        FROM revenue_poc_positions ORDER BY id
        """
    ).fetchall()
    open_positions = [row for row in positions if row[1] == "OPEN"]
    resolved = [row for row in positions if row[1] == "RESOLVED"]
    deployed = sum(float(row[2]) for row in open_positions)
    realized = sum(float(row[7] or 0.0) for row in resolved)
    realized_fees = sum(float(row[9] or 0.0) for row in resolved)
    realized_slippage = sum(float(row[10] or 0.0) for row in resolved)
    open_fees = sum(float(row[3]) for row in open_positions)
    open_slippage = sum(float(row[4]) for row in open_positions)
    spread_cost = sum(float(row[5]) for row in positions)
    starting = float(account[0])
    cash = starting + realized - deployed - open_fees - open_slippage
    unrealized = 0.0
    marked_positions = 0
    marks: list[dict[str, Any]] = []
    for row in open_positions:
        mark = conn.execute(
            """
            SELECT quote_timestamp_utc,executable_bid,executable_ask,bid_depth_usd,
                   ask_depth_usd,depth_source,fee_source,quote_source,mark_price,
                   market_value_usd,gross_unrealized_pnl_usd,estimated_exit_fee_usd,
                   estimated_exit_slippage_usd,unrealized_pnl_usd,assumptions_json
            FROM revenue_poc_marks WHERE position_id=?
            ORDER BY quote_timestamp_utc DESC,id DESC LIMIT 1
            """,
            (row[0],),
        ).fetchone()
        if mark:
            marked_positions += 1
            unrealized += float(mark[13])
            marks.append(
                {
                    "position_id": int(row[0]),
                    "quote_timestamp_utc": mark[0],
                    "executable_bid": float(mark[1]),
                    "executable_ask": float(mark[2]),
                    "bid_depth_usd": mark[3],
                    "ask_depth_usd": mark[4],
                    "depth_source": mark[5],
                    "fee_source": mark[6],
                    "quote_source": mark[7],
                    "mark_price": float(mark[8]),
                    "market_value_usd": round(float(mark[9]), 4),
                    "gross_unrealized_pnl_usd": round(float(mark[10]), 4),
                    "estimated_exit_fee_usd": round(float(mark[11]), 4),
                    "estimated_exit_slippage_usd": round(float(mark[12]), 4),
                    "unrealized_pnl_usd": round(float(mark[13]), 4),
                    "assumptions": json.loads(mark[14]),
                }
            )
    equity = cash + deployed + unrealized
    wins = [row for row in resolved if float(row[7] or 0) > 0]
    losses = [row for row in resolved if float(row[7] or 0) < 0]
    gross_profit = sum(float(row[7]) for row in wins)
    gross_loss = abs(sum(float(row[7]) for row in losses))
    max_drawdown = float(
        conn.execute("SELECT COALESCE(MAX(drawdown_usd),0) FROM revenue_poc_equity_points").fetchone()[0]
    )
    evaluations = int(conn.execute("SELECT COUNT(*) FROM revenue_poc_evaluations").fetchone()[0])
    candidates = int(
        conn.execute(
            "SELECT COALESCE(SUM(candidates),0) FROM revenue_poc_api_daily"
        ).fetchone()[0]
    )
    api = conn.execute(
        """
        SELECT COALESCE(SUM(api_calls),0),COALESCE(SUM(input_tokens),0),
               COALESCE(SUM(output_tokens),0),
               COALESCE(SUM(cache_creation_input_tokens),0),
               COALESCE(SUM(cache_read_input_tokens),0),SUM(estimated_cost_usd),
               COALESCE(SUM(unknown_cost_calls),0),COALESCE(SUM(cache_hits),0),
               COALESCE(SUM(calls_avoided),0),COALESCE(SUM(markets_evaluated),0),
               COALESCE(SUM(candidates),0),COALESCE(SUM(admitted_trades),0)
        FROM revenue_poc_api_daily
        """
    ).fetchone()
    latest_api = conn.execute(
        "SELECT date_utc,estimated_cost_usd,unknown_cost_calls "
        "FROM revenue_poc_api_daily ORDER BY date_utc DESC LIMIT 1"
    ).fetchone()
    known_cost = float(api[5]) if api[5] is not None else None
    unknown_cost_calls = int(api[6])
    admitted = len(positions)
    tables = {
        str(row[0])
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    budget = {
        "spent_usd": 0.0,
        "reserved_usd": 0.0,
        "remaining_usd": None,
        "calls_skipped_by_cost": 0,
        "calls_skipped_by_emergency": 0,
        "modeled_ev_skipped_usd": 0.0,
        "calls_by_model": {},
        "cache_savings_usd": 0.0,
    }
    if "revenue_poc_budget_events" in tables:
        row = conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN status='RESERVED' THEN reserved_cost_usd ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='SKIPPED_COST' THEN 1 ELSE 0 END),0),
                      COALESCE(SUM(CASE WHEN status='SKIPPED_EMERGENCY' THEN 1 ELSE 0 END),0),
                      COALESCE(SUM(modeled_ev_skipped_usd),0)
               FROM revenue_poc_budget_events WHERE date_utc=?""",
            (latest_api[0] if latest_api else "",),
        ).fetchone()
        if row:
            budget["reserved_usd"] = float(row[0])
            budget["calls_skipped_by_cost"] = int(row[1])
            budget["calls_skipped_by_emergency"] = int(row[2])
            budget["modeled_ev_skipped_usd"] = float(row[3])
    if "revenue_poc_api_calls" in tables:
        model_rows = conn.execute(
            "SELECT COALESCE(model,'unknown'),COUNT(*) FROM revenue_poc_api_calls GROUP BY model"
        ).fetchall()
        budget["calls_by_model"] = {str(row[0]): int(row[1]) for row in model_rows}
        budget["cache_savings_usd"] = float(
            conn.execute(
                "SELECT COALESCE(SUM(estimated_cache_savings_usd),0) FROM revenue_poc_api_calls"
            ).fetchone()[0]
        )
        hour_rows = conn.execute(
            """SELECT substr(timestamp_utc,1,13) AS hour,
                      ROUND(COALESCE(SUM(estimated_cost_usd),0),8),COUNT(*)
               FROM revenue_poc_api_calls GROUP BY hour ORDER BY hour DESC LIMIT 168"""
        ).fetchall()
        budget["spend_by_hour"] = [
            {"hour_utc": str(row[0]), "spend_usd": float(row[1] or 0), "calls": int(row[2])}
            for row in hour_rows
        ]
        cycle_rows = conn.execute(
            """SELECT substr(timestamp_utc,1,16) AS cycle,
                      ROUND(COALESCE(SUM(estimated_cost_usd),0),8),COUNT(*)
               FROM revenue_poc_api_calls GROUP BY cycle ORDER BY cycle DESC LIMIT 100"""
        ).fetchall()
        budget["spend_by_cycle"] = [
            {"cycle_utc": str(row[0]), "spend_usd": float(row[1] or 0), "calls": int(row[2])}
            for row in cycle_rows
        ]
    return {
        "portfolio": {
            "starting_balance_usd": starting,
            "cash_usd": round(cash, 4),
            "deployed_capital_usd": round(deployed, 4),
            "equity_usd": round(equity, 4),
            "open_positions": len(open_positions),
            "resolved_positions": len(resolved),
        },
        "performance": {
            "realized_pnl_usd": round(realized, 4),
            "unrealized_pnl_usd": unrealized,
            "unrealized_marking_status": (
                "current_executable_marks" if marked_positions == len(open_positions)
                else "partial_marks_with_unmarked_positions_at_entry"
            ),
            "net_return": _ratio(equity - starting, starting),
            "win_rate": _ratio(len(wins), len(resolved)),
            "profit_factor": _ratio(gross_profit, gross_loss),
            "average_winner_usd": _ratio(gross_profit, len(wins)),
            "average_loser_usd": _ratio(-gross_loss, len(losses)),
            "maximum_drawdown_usd": round(max_drawdown, 4),
            "expected_value_open_usd": round(sum(float(row[6]) for row in open_positions), 4),
        },
        "execution": {
            "realized_fees_usd": round(realized_fees, 4),
            "realized_slippage_usd": round(realized_slippage, 4),
            "open_entry_fees_usd": round(open_fees, 4),
            "open_entry_slippage_usd": round(open_slippage, 4),
            "spread_cost_usd": round(spread_cost, 4),
            "capital_utilization": _ratio(deployed, starting),
            "candidate_conversion": _ratio(admitted, candidates),
            "opportunity_conversion": _ratio(admitted, evaluations),
        },
        "api": {
            "api_calls": int(api[0]),
            "known_api_spend_usd": round(known_cost, 6) if known_cost is not None else None,
            "unknown_cost_calls": unknown_cost_calls,
            "input_tokens": int(api[1]),
            "output_tokens": int(api[2]),
            "token_measurement": "unavailable_for_legacy_shadow_rows" if not (api[1] or api[2]) else "observed",
            "cache_creation_input_tokens": int(api[3]),
            "cache_read_input_tokens": int(api[4]),
            "cache_savings_calls": int(api[8]),
            "cache_hits": int(api[7]),
            "calls_avoided": int(api[8]),
            "markets_evaluated": int(api[9]),
            "candidates": int(api[10]),
            "admitted_trades": int(api[11]),
            "cost_per_evaluated_market": (
                _ratio(known_cost, evaluations) if known_cost is not None and not unknown_cost_calls else None
            ),
            "cost_per_candidate": (
                _ratio(known_cost, candidates) if known_cost is not None and not unknown_cost_calls else None
            ),
            "cost_per_admitted_trade": (
                _ratio(known_cost, admitted) if known_cost is not None and not unknown_cost_calls else None
            ),
            "latest_budget_date_utc": latest_api[0] if latest_api else None,
            "remaining_daily_budget_usd": round(
                max(0.0, float(account[1]) - float(latest_api[1])), 6
            ) if (
                latest_api
                and latest_api[1] is not None
                and int(latest_api[2] or 0) == 0
            ) else None,
            "cost_measurement": (
                "complete_observed_token_estimate" if not unknown_cost_calls
                else "partial_with_historical_unknowns"
            ),
            "budget": budget,
            "calls_by_model": budget["calls_by_model"],
            "provider_cache_savings_usd": round(budget["cache_savings_usd"], 8),
        },
        "optimization": {
            "thresholds": _threshold_summary(conn, tables),
            "discovery": _discovery_summary(conn, tables),
            "quarantine": _quarantine_summary(conn, tables),
        },
        "current_marks": marks,
        "equity_curve": [
            {
                "timestamp_utc": row[0], "event_type": row[1],
                "cash_usd": round(float(row[2]), 4),
                "deployed_capital_usd": round(float(row[3]), 4),
                "realized_pnl_usd": round(float(row[4]), 4),
                "unrealized_pnl_usd": round(float(row[5]), 4),
                "equity_usd": round(float(row[6]), 4),
                "drawdown_usd": round(float(row[7]), 4),
            }
            for row in conn.execute(
                "SELECT timestamp_utc,event_type,cash_usd,deployed_capital_usd,realized_pnl_usd,unrealized_pnl_usd,equity_usd,drawdown_usd FROM revenue_poc_equity_points ORDER BY id"
            ).fetchall()
        ],
    }


def _threshold_summary(conn: sqlite3.Connection, tables: set[str]) -> list[dict[str, Any]]:
    if "revenue_poc_shadow_thresholds" not in tables:
        return []
    rows = conn.execute(
        """SELECT threshold_label,threshold,COUNT(*),SUM(qualifies),
                  ROUND(SUM(modeled_ev_usd),4),ROUND(SUM(capital_required_usd),4)
           FROM revenue_poc_shadow_thresholds GROUP BY threshold_label,threshold
           ORDER BY threshold"""
    ).fetchall()
    return [
        {"label": str(row[0]), "threshold": float(row[1]), "evaluations": int(row[2]),
         "qualifying": int(row[3] or 0), "modeled_ev_usd": float(row[4] or 0),
         "capital_required_usd": float(row[5] or 0)}
        for row in rows
    ]


def _discovery_summary(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    if "revenue_poc_discovery_snapshots" not in tables:
        return {}
    row = conn.execute(
        """SELECT COUNT(*),COUNT(DISTINCT market_id),
                  SUM(status='VALID'),SUM(dynamic_shortlist=1),
                  SUM(fixed_watchlist=0 AND dynamic_shortlist=1),
                  ROUND(SUM(CASE WHEN dynamic_shortlist=1 THEN modeled_ev_usd ELSE 0 END),4)
           FROM revenue_poc_discovery_snapshots"""
    ).fetchone()
    return {"rows": int(row[0]), "unique_contracts": int(row[1]), "valid": int(row[2] or 0),
            "shortlisted": int(row[3] or 0), "outside_fixed_watchlist": int(row[4] or 0),
            "modeled_ev_outside_fixed_usd": float(row[5] or 0)}


def _quarantine_summary(conn: sqlite3.Connection, tables: set[str]) -> dict[str, Any]:
    if "revenue_poc_market_health" not in tables:
        return {}
    rows = conn.execute(
        "SELECT status,COUNT(*) FROM revenue_poc_market_health GROUP BY status"
    ).fetchall()
    return {str(row[0]).lower(): int(row[1]) for row in rows}


def funnel_analysis(conn: sqlite3.Connection, pipeline_path: Path | None = None) -> dict[str, Any]:
    pipeline: dict[str, Any] = {}
    if pipeline_path and pipeline_path.exists():
        parsed = json.loads(pipeline_path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            pipeline = parsed
    reasons = [
        {
            "reason": row[0],
            "unique_contracts": int(row[1]),
            "expected_lost_pnl_usd": round(float(row[2] or 0), 4),
            "method": "maximum modeled EV per rejected contract; not realized P&L",
        }
        for row in conn.execute(
            """
            SELECT reason,COUNT(*),SUM(max_ev) FROM (
              SELECT d.reason,e.venue,e.market_id,MAX(d.expected_lost_pnl_usd) max_ev
              FROM revenue_poc_decisions d
              JOIN revenue_poc_evaluations e ON e.id=d.evaluation_id
              WHERE d.decision='REJECT'
              GROUP BY d.reason,e.venue,e.market_id
            ) GROUP BY reason ORDER BY SUM(max_ev) DESC,COUNT(*) DESC
            """
        ).fetchall()
    ]
    production = [
        {"decision": row[0], "reason": row[1] or "admitted", "count": int(row[2]), "raw_edge_dollars_proxy": round(float(row[3] or 0), 4)}
        for row in conn.execute(
            "SELECT production_decision,production_rejection_reason,COUNT(*),SUM(raw_edge*25.0) FROM revenue_poc_evaluations GROUP BY production_decision,production_rejection_reason ORDER BY SUM(raw_edge*25.0) DESC"
        ).fetchall()
    ]
    return {
        "latest_pipeline_summary": pipeline.get("summary", {}),
        "production_historical_conversion": production,
        "revenue_rejections_ranked_by_expected_lost_pnl": reasons,
        "evidence_limits": [
            "Early universe/policy rejections lack model probabilities, so expected lost P&L is not estimable.",
            "Imported shadow history includes resolved snapshots, but no admitted Revenue POC contract matched a resolved snapshot in this validation; expected value remains model-derived.",
            "Depth uses recorded liquidity as a proxy because order-book depth was not historically persisted.",
        ],
    }


def alpha_attribution_report(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return reconciled, immutable Revenue alpha attribution evidence."""
    resolved = int(conn.execute("SELECT COUNT(*) FROM revenue_poc_positions WHERE status='RESOLVED'").fetchone()[0])
    entries = int(conn.execute("SELECT COUNT(*) FROM revenue_poc_attribution_entries").fetchone()[0])
    completed = int(conn.execute("SELECT COUNT(*) FROM revenue_poc_attribution_completions").fetchone()[0])
    rows = []
    for row in conn.execute(
        """
        SELECT a.strategy_id,a.strategy_version,a.source_id,a.source_type,a.event_family_id,
               a.category,a.horizon_bucket,a.entry_benchmark_price,
               c.exit_benchmark_price,c.settlement_outcome,c.resolved_at_utc,c.capital_days,
               c.fees_usd,c.slippage_usd,c.close_classification,c.forecast_alpha_usd,
               c.event_alpha_usd,c.resolution_alpha_usd,c.structural_alpha_usd,
               c.realized_net_pnl_usd,c.attribution_status
        FROM revenue_poc_attribution_entries a
        JOIN revenue_poc_attribution_completions c ON c.entry_id=a.id
        ORDER BY c.id
        """
    ).fetchall():
        components = [float(row[index] or 0.0) for index in (15, 16, 17, 18)]
        realized = float(row[19])
        rows.append({
            "strategy_id": row[0], "strategy_version": row[1], "source_id": row[2],
            "source_type": row[3], "event_family_id": row[4], "category": row[5],
            "horizon_bucket": row[6], "entry_benchmark_price": round(float(row[7]), 8),
            "exit_benchmark_price": row[8], "settlement_outcome": row[9],
            "resolved_at_utc": row[10], "capital_days": round(float(row[11]), 6),
            "fees_usd": round(float(row[12]), 6), "slippage_usd": round(float(row[13]), 6),
            "close_classification": row[14],
            "forecast_alpha_usd": round(components[0], 6), "event_alpha_usd": round(components[1], 6),
            "resolution_alpha_usd": round(components[2], 6), "structural_alpha_usd": round(components[3], 6),
            "realized_net_pnl_usd": round(realized, 6),
            "reconciliation_residual_usd": round(sum(components) - realized, 8),
            "attribution_status": row[20],
        })
    residual = round(sum(item["reconciliation_residual_usd"] for item in rows), 8)
    complete_coverage = (completed / resolved) if resolved else None
    return {
        "coverage": {
            "resolved_positions": resolved,
            "decision_entries": entries,
            "completed_attributions": completed,
            "resolved_position_coverage": round(complete_coverage, 6) if complete_coverage is not None else None,
            "target": 0.95,
            "target_met": bool(complete_coverage is not None and complete_coverage >= 0.95),
        },
        "reconciliation": {
            "attribution_rows": len(rows),
            "total_residual_usd": residual,
            "within_one_cent": abs(residual) <= 0.01,
        },
        "attributions": rows,
        "limitations": [
            "Event alpha is zero-labelled until a peer/event-family benchmark is available.",
            "Missing marks produce PARTIAL_PROXY completion status and use settlement as the exit proxy.",
            "Source and event-family identifiers fall back to shadow forecast or market identifiers when absent.",
            "Existing historical positions are not rewritten; they remain uncovered unless a new immutable completion record exists.",
        ],
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
