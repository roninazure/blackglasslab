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


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
