#!/usr/bin/env python3
"""
watch_resolutions.py — Phase 2.4 resolution alert watcher.

Scans paper_trades for resolved non-FAKE markets and prints a P&L summary.
Run manually after resolve_paper_trades.py to see first real data points.

Usage:
    python3 scripts/watch_resolutions.py
    python3 scripts/watch_resolutions.py --since 2026-03-01
    python3 scripts/watch_resolutions.py --db memory/runs.sqlite --tail 50
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional


DB_PATH = os.path.join("memory", "runs.sqlite")


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json(s: Optional[str]) -> Dict[str, Any]:
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="Black Glass Swarm — Resolution Watcher")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--since", default=None, help="Only show resolutions on or after YYYY-MM-DD UTC")
    ap.add_argument("--tail", type=int, default=50, help="Max resolved trades to display")
    ap.add_argument("--venue", default="polymarket")
    args = ap.parse_args()

    conn = _connect_db(args.db)
    cur = conn.cursor()

    clauses = [
        "status='CLOSED'",
        "resolved_outcome IS NOT NULL",
        "market_id NOT LIKE 'FAKE-%'",
        f"venue='{args.venue}'",
    ]
    if args.since:
        clauses.append(f"ts_utc >= '{args.since}'")
    where = "WHERE " + " AND ".join(clauses)

    cur.execute(
        f"""
        SELECT id, ts_utc, market_id, side, size_usd, consensus_p_yes,
               resolved_outcome, brier, notes
        FROM paper_trades
        {where}
        ORDER BY id DESC
        LIMIT ?;
        """,
        (args.tail,),
    )
    rows = cur.fetchall()

    cur.execute(
        f"""
        SELECT
            COUNT(*) AS total,
            SUM(CASE WHEN resolved_outcome='YES' THEN 1 ELSE 0 END) AS yes_count,
            SUM(CASE WHEN resolved_outcome='NO'  THEN 1 ELSE 0 END) AS no_count,
            AVG(brier) AS avg_brier
        FROM paper_trades
        {where};
        """
    )
    summary = cur.fetchone()

    now = datetime.now(timezone.utc).isoformat()
    print(f"BLACK GLASS SWARM — RESOLUTION WATCHER (Phase 2.4)")
    print(f"- generated_at_utc: {now}")
    print(f"- db: {args.db}")
    print(f"- venue: {args.venue}")
    if args.since:
        print(f"- since: {args.since}")
    print()

    total = summary["total"] or 0
    avg_b = summary["avg_brier"]
    print("SUMMARY")
    print(f"- resolved_trades:  {total}")
    print(f"- resolved_YES:     {summary['yes_count'] or 0}")
    print(f"- resolved_NO:      {summary['no_count'] or 0}")
    print(f"- avg_brier:        {avg_b:.6f}" if avg_b is not None else "- avg_brier:        -")

    # P&L rollup
    total_wagered = 0.0
    total_profit = 0.0
    profit_known = 0
    for r in rows:
        n = _safe_json(r["notes"])
        size = float(r["size_usd"] or 100.0)
        total_wagered += size
        # Try resolver-written profit first, then compute on the fly
        profit = n.get("profit_usd")
        if profit is None:
            res = n.get("resolution") or {}
            profit = res.get("profit_usd")
        if profit is None:
            side = str(r["side"] or "YES").upper()
            outcome = str(r["resolved_outcome"] or "").upper()
            p_mkt = n.get("p_yes_market")
            correct = (side == "YES" and outcome == "YES") or (side == "NO" and outcome == "NO")
            if p_mkt is not None:
                p = max(0.001, min(0.999, float(p_mkt)))
                if correct:
                    profit = size * (1.0 / p - 1.0) if side == "YES" else size * (p / (1.0 - p))
                else:
                    profit = -size
            else:
                profit = size if correct else -size
        total_profit += float(profit)
        profit_known += 1

    if total_wagered > 0:
        roi = total_profit / total_wagered * 100.0
        print(f"- total_wagered:    ${total_wagered:,.2f}")
        print(f"- total_profit:     ${total_profit:+,.2f}")
        print(f"- roi:              {roi:+.1f}%")
    print()

    if not rows:
        print("🔴 No resolved non-FAKE trades yet — waiting for first real data point.")
        conn.close()
        return 0

    print(f"RESOLVED TRADES (showing {len(rows)} of {total})")
    print(f"  {'id':>5}  {'ts':19}  {'side':3}  {'outcome':7}  {'brier':8}  {'profit':>10}  market")
    print(f"  {'-'*5}  {'-'*19}  {'-'*3}  {'-'*7}  {'-'*8}  {'-'*10}  {'-'*40}")
    for r in rows:
        n = _safe_json(r["notes"])
        side = str(r["side"] or "?")
        outcome = str(r["resolved_outcome"] or "?")
        correct = (side.upper() == "YES" and outcome.upper() == "YES") or (
            side.upper() == "NO" and outcome.upper() == "NO"
        )
        flag = "✓" if correct else "✗"
        brier_s = f"{r['brier']:.6f}" if r["brier"] is not None else "       -"
        # get profit
        profit = n.get("profit_usd") or (n.get("resolution") or {}).get("profit_usd")
        profit_s = f"${float(profit):+,.2f}" if profit is not None else "      ?"
        ts = (r["ts_utc"] or "")[:19]
        market = (r["market_id"] or "")[:40]
        print(f"  {r['id']:>5}  {ts}  {side:3}  {outcome:7}  {brier_s}  {profit_s:>10}  {market} {flag}")

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
