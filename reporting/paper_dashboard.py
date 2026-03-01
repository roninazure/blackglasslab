#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional, List, Tuple

DB_PATH = os.path.join("memory", "runs.sqlite")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _fmt(x: Any, nd: int = 6) -> str:
    if x is None:
        return "-"
    try:
        return f"{float(x):.{nd}f}"
    except Exception:
        return str(x)


def _extract_market_fields(notes: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """
    Returns (p_yes_market, edge_vs_market_signed).
    """
    p_yes_market = notes.get("p_yes_market")
    edge_vs_market = notes.get("edge_vs_market")
    try:
        p_yes_market = float(p_yes_market) if p_yes_market is not None else None
    except Exception:
        p_yes_market = None
    try:
        edge_vs_market = float(edge_vs_market) if edge_vs_market is not None else None
    except Exception:
        edge_vs_market = None
    return p_yes_market, edge_vs_market


def quality_score(edge_abs: float, disagree: float) -> float:
    """
    Explainable, bounded-ish quality score.

    - edge_abs: |model - market| at entry (primary signal)
    - disagree: swarm disagreement (uncertainty / instability)

    Score favors high edge, penalizes disagreement.

    score = edge_abs * (1 - 0.75*disagree)

    If disagree=0.0 => score=edge
    If disagree=1.0 => score=edge*0.25
    """
    d = max(0.0, min(1.0, float(disagree)))
    e = max(0.0, float(edge_abs))
    return e * (1.0 - 0.75 * d)


def tier(score: float) -> str:
    # Tuned to your current observed edge scale (~0.00–0.12)
    if score >= 0.070:
        return "A"
    if score >= 0.040:
        return "B"
    if score >= 0.020:
        return "C"
    return "D"


def main() -> int:
    ap = argparse.ArgumentParser(description="Black Glass Swarm — Paper Trading Dashboard")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--venue", default="polymarket")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--include-fake", action="store_true")

    # 1.6C filters (actionable signals)
    ap.add_argument("--min-edge-vs-market", type=float, default=0.03,
                    help="Minimum |edge_vs_market| required to treat as actionable (default 0.03 = 3%%).")
    ap.add_argument("--min-edge-abs", type=float, default=0.03,
                    help="Minimum edge_abs required (fallback when snapshot missing).")
    ap.add_argument("--max-disagree", type=float, default=0.60,
                    help="Maximum disagreement allowed for actionable signals.")
    ap.add_argument("--max-actionable", type=int, default=10)

    args = ap.parse_args()

    conn = _connect_db(args.db)
    cur = conn.cursor()

    where = "WHERE venue=?"
    params: List[Any] = [args.venue]
    if not args.include_fake:
        where += " AND market_id NOT LIKE 'FAKE-%'"

    # topline
    cur.execute(f"SELECT COUNT(*) AS n FROM paper_trades {where};", params)
    total = int(cur.fetchone()["n"] or 0)

    cur.execute(f"SELECT COUNT(*) AS n FROM paper_trades {where} AND status='OPEN';", params)
    open_n = int(cur.fetchone()["n"] or 0)

    cur.execute(f"SELECT COUNT(*) AS n FROM paper_trades {where} AND status!='OPEN';", params)
    closed_n = int(cur.fetchone()["n"] or 0)

    cur.execute(f"SELECT AVG(edge) AS a FROM paper_trades {where};", params)
    avg_edge = cur.fetchone()["a"]

    cur.execute(f"SELECT AVG(disagreement) AS a FROM paper_trades {where};", params)
    avg_disagree = cur.fetchone()["a"]

    cur.execute(f"SELECT AVG(brier) AS a FROM paper_trades {where} AND status!='OPEN' AND brier IS NOT NULL;", params)
    avg_brier = cur.fetchone()["a"]

    cur.execute(f"SELECT MIN(ts_utc) AS t FROM paper_trades {where};", params)
    first_ts = cur.fetchone()["t"]

    cur.execute(f"SELECT MAX(ts_utc) AS t FROM paper_trades {where};", params)
    last_ts = cur.fetchone()["t"]

    # reason breakdown
    cur.execute(
        f"""
        SELECT reason, COUNT(*) AS n, AVG(edge) AS avg_edge, AVG(disagreement) AS avg_disagree
        FROM paper_trades
        {where}
        GROUP BY reason
        ORDER BY n DESC;
        """,
        params,
    )
    reasons = cur.fetchall()

    # latest trades
    cur.execute(
        f"""
        SELECT id, ts_utc, market_id, question, side, consensus_p_yes, edge, disagreement, status, resolved_outcome, brier, notes, reason
        FROM paper_trades
        {where}
        ORDER BY id DESC
        LIMIT ?;
        """,
        params + [args.limit],
    )
    latest = cur.fetchall()

    # build actionable + quality lists from recent OPEN trades
    cur.execute(
        f"""
        SELECT id, ts_utc, market_id, question, side, consensus_p_yes, edge, disagreement, status, notes, reason
        FROM paper_trades
        {where} AND status='OPEN'
        ORDER BY id DESC
        LIMIT 400;
        """,
        params,
    )
    open_recent = cur.fetchall()

    actionable: List[Tuple[float, sqlite3.Row, Optional[float], Optional[float]]] = []
    all_scored: List[Tuple[float, sqlite3.Row, Optional[float], Optional[float]]] = []

    for r in open_recent:
        notes = _safe_json(r["notes"])
        p_yes_mkt, evm = _extract_market_fields(notes)

        edge_abs = float(r["edge"] or 0.0)
        disagree = float(r["disagreement"] or 0.0)
        q = quality_score(edge_abs=edge_abs, disagree=disagree)

        all_scored.append((q, r, p_yes_mkt, evm))

        # Actionable criteria:
        # - disagreement <= max
        # - if we have edge_vs_market: |evm| >= min_edge_vs_market
        # - else: edge_abs >= min_edge_abs
        if disagree > args.max_disagree:
            continue

        if evm is not None:
            if abs(float(evm)) < args.min_edge_vs_market:
                continue
        else:
            if edge_abs < args.min_edge_abs:
                continue

        actionable.append((q, r, p_yes_mkt, evm))

    actionable.sort(key=lambda t: t[0], reverse=True)
    all_scored.sort(key=lambda t: t[0], reverse=True)

    # tier counts (open)
    tier_counts = {"A": 0, "B": 0, "C": 0, "D": 0}
    for q, r, _, _ in all_scored:
        tier_counts[tier(q)] += 1

    # Print
    print(f"BLACK GLASS SWARM — PAPER DASHBOARD (Phase 1.6C)")
    print(f"- generated_at_utc: {utc_now_iso()}")
    print(f"- db: {args.db}")
    print(f"- venue: {args.venue}")
    print(f"- include_fake: {bool(args.include_fake)}")
    print(f"- actionable_filters: min_edge_vs_market={args.min_edge_vs_market} min_edge_abs={args.min_edge_abs} max_disagree={args.max_disagree}")
    print()

    print("TOPLINE")
    print(f"- total_trades:      {total}")
    print(f"- open_trades:       {open_n}")
    print(f"- closed_trades:     {closed_n}")
    print(f"- avg_edge_abs:      {_fmt(avg_edge, 6)}")
    print(f"- avg_disagreement:  {_fmt(avg_disagree, 6)}")
    print(f"- avg_brier_closed:  {_fmt(avg_brier, 6)}")
    print(f"- first_ts:          {first_ts or '-'}")
    print(f"- last_ts:           {last_ts or '-'}")
    print()

    print("SIGNAL QUALITY (OPEN) — Tier Counts")
    print(f"- A (>=0.070): {tier_counts['A']}")
    print(f"- B (>=0.040): {tier_counts['B']}")
    print(f"- C (>=0.020): {tier_counts['C']}")
    print(f"- D (<0.020):  {tier_counts['D']}")
    print()

    print("REASON BREAKDOWN")
    if not reasons:
        print("- (none)")
    else:
        for r in reasons:
            print(f"- {r['reason']}: n={r['n']} avg_edge_abs={_fmt(r['avg_edge'])} avg_disagree={_fmt(r['avg_disagree'])}")
    print()

    print("ACTIONABLE SIGNALS (OPEN) — ranked by QualityScore = edge_abs * (1 - 0.75*disagree)")
    if not actionable:
        print("- (none passed filters)")
    else:
        for q, r, p_yes_mkt, evm in actionable[: args.max_actionable]:
            notes = _safe_json(r["notes"])
            snap = notes.get("snapshot") if isinstance(notes.get("snapshot"), dict) else {}
            updated_at = snap.get("updatedAt")
            print(
                f"- tier={tier(q)} q={_fmt(q,6)} id={r['id']} market={r['market_id']} side={r['side']} "
                f"model_yes={_fmt(r['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} edge_vs_mkt={_fmt(evm)} "
                f"edge_abs={_fmt(r['edge'])} disagree={_fmt(r['disagreement'])} updatedAt={updated_at or '-'}"
            )
    print()

    print("LATEST TRADES")
    if not latest:
        print("- (none)")
    else:
        for r in latest:
            notes = _safe_json(r["notes"])
            p_yes_mkt, evm = _extract_market_fields(notes)
            q = quality_score(edge_abs=float(r["edge"] or 0.0), disagree=float(r["disagreement"] or 0.0))
            print(
                f"- tier={tier(q)} q={_fmt(q,6)} id={r['id']} ts={r['ts_utc']} market={r['market_id']} side={r['side']} "
                f"model_yes={_fmt(r['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} edge_vs_mkt={_fmt(evm)} "
                f"edge_abs={_fmt(r['edge'])} disagree={_fmt(r['disagreement'])} status={r['status']}"
            )
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
