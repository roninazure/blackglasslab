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
        "SELECT status,size_usd,fee_usd,slippage_usd,spread_cost_usd,expected_value_usd,realized_pnl_usd,side,resolved_outcome FROM revenue_poc_positions ORDER BY id"
    ).fetchall()
    open_positions = [row for row in positions if row[0] == "OPEN"]
    resolved = [row for row in positions if row[0] == "RESOLVED"]
    deployed = sum(float(row[1]) for row in open_positions)
    realized = sum(float(row[6] or 0.0) for row in resolved)
    fees = sum(float(row[2]) for row in positions)
    slippage = sum(float(row[3]) for row in positions)
    spread_cost = sum(float(row[4]) for row in positions)
    starting = float(account[0])
    cash = starting + realized - deployed - sum(float(row[2]) + float(row[3]) for row in open_positions)
    unrealized = 0.0
    equity = cash + deployed + unrealized
    wins = [row for row in resolved if float(row[6] or 0) > 0]
    losses = [row for row in resolved if float(row[6] or 0) < 0]
    gross_profit = sum(float(row[6]) for row in wins)
    gross_loss = abs(sum(float(row[6]) for row in losses))
    peak = starting
    equity_path = starting
    max_drawdown = 0.0
    for row in resolved:
        equity_path += float(row[6] or 0.0)
        peak = max(peak, equity_path)
        max_drawdown = max(max_drawdown, peak - equity_path)
    evaluations = int(conn.execute("SELECT COUNT(*) FROM revenue_poc_evaluations").fetchone()[0])
    candidates = int(
        conn.execute(
            "SELECT COALESCE(SUM(candidates),0) FROM revenue_poc_api_daily"
        ).fetchone()[0]
    )
    api = conn.execute(
        "SELECT COALESCE(SUM(api_calls),0),COALESCE(SUM(input_tokens),0),COALESCE(SUM(output_tokens),0),COALESCE(SUM(estimated_cost_usd),0),COALESCE(SUM(cache_hits),0),COALESCE(SUM(calls_avoided),0),COALESCE(SUM(markets_evaluated),0),COALESCE(SUM(candidates),0),COALESCE(SUM(admitted_trades),0) FROM revenue_poc_api_daily"
    ).fetchone()
    latest_api = conn.execute(
        "SELECT date_utc,estimated_cost_usd FROM revenue_poc_api_daily ORDER BY date_utc DESC LIMIT 1"
    ).fetchone()
    cost = float(api[3])
    admitted = len(positions)
    return {
        "portfolio": {
            "starting_balance_usd": starting,
            "cash_usd": round(cash, 4),
            "deployed_capital_usd": round(deployed, 4),
            "equity_usd": round(equity, 4),
            "open_positions": len(open_positions),
        },
        "performance": {
            "realized_pnl_usd": round(realized, 4),
            "unrealized_pnl_usd": unrealized,
            "unrealized_marking_status": "entry_only_no_current_marks",
            "net_return": _ratio(equity - starting, starting),
            "win_rate": _ratio(len(wins), len(resolved)),
            "profit_factor": _ratio(gross_profit, gross_loss),
            "average_winner_usd": _ratio(gross_profit, len(wins)),
            "average_loser_usd": _ratio(-gross_loss, len(losses)),
            "maximum_drawdown_usd": round(max_drawdown, 4),
            "expected_value_open_usd": round(sum(float(row[5]) for row in open_positions), 4),
        },
        "execution": {
            "fees_usd": round(fees, 4),
            "slippage_usd": round(slippage, 4),
            "spread_cost_usd": round(spread_cost, 4),
            "capital_utilization": _ratio(deployed, starting),
            "candidate_conversion": _ratio(admitted, candidates),
            "opportunity_conversion": _ratio(admitted, evaluations),
        },
        "api": {
            "api_calls": int(api[0]),
            "api_spend_usd": round(cost, 6),
            "input_tokens": int(api[1]),
            "output_tokens": int(api[2]),
            "token_measurement": "unavailable_for_legacy_shadow_rows" if not (api[1] or api[2]) else "observed",
            "cache_savings_calls": int(api[5]),
            "cache_hits": int(api[4]),
            "calls_avoided": int(api[5]),
            "markets_evaluated": int(api[6]),
            "candidates": int(api[7]),
            "admitted_trades": int(api[8]),
            "cost_per_evaluated_market": _ratio(cost, evaluations),
            "cost_per_candidate": _ratio(cost, candidates),
            "cost_per_admitted_trade": _ratio(cost, admitted),
            "latest_budget_date_utc": latest_api[0] if latest_api else None,
            "remaining_daily_budget_usd": round(
                max(0.0, float(account[1]) - float(latest_api[1] if latest_api else 0.0)), 6
            ),
            "cost_measurement": "unavailable_configured_zero" if cost == 0 else "configured_estimate",
        },
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
            "All imported shadow forecasts are unresolved; expected value is model-derived, not realized P&L.",
            "Depth uses recorded liquidity as a proxy because order-book depth was not historically persisted.",
        ],
    }


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
