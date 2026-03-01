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


def _extract_market_fields(notes: Dict[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """
    Returns (p_yes_market, edge_vs_market, model_yes_recomputed_if_possible)
    """
    p_yes_market = notes.get("p_yes_market")
    edge_vs_market = notes.get("edge_vs_market")

    # model_yes can be recomputed if both exist; otherwise None
    model_yes = None
    try:
        if p_yes_market is not None and edge_vs_market is not None:
            model_yes = float(p_yes_market) + float(edge_vs_market)
    except Exception:
        model_yes = None
    return (
        float(p_yes_market) if p_yes_market is not None else None,
        float(edge_vs_market) if edge_vs_market is not None else None,
        model_yes,
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Black Glass Swarm — Paper Trading Dashboard")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--venue", default="polymarket")
    ap.add_argument("--limit", type=int, default=12)
    ap.add_argument("--include-fake", action="store_true")
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

    # open trades sorted by edge
    cur.execute(
        f"""
        SELECT id, market_id, side, consensus_p_yes, edge, disagreement, reason, notes
        FROM paper_trades
        {where} AND status='OPEN'
        ORDER BY edge DESC
        LIMIT 8;
        """,
        params,
    )
    top_edge = cur.fetchall()

    cur.execute(
        f"""
        SELECT id, market_id, side, consensus_p_yes, edge, disagreement, reason, notes
        FROM paper_trades
        {where} AND status='OPEN'
        ORDER BY edge ASC
        LIMIT 8;
        """,
        params,
    )
    low_edge = cur.fetchall()

    cur.execute(
        f"""
        SELECT id, market_id, side, consensus_p_yes, edge, disagreement, reason, notes
        FROM paper_trades
        {where} AND status='OPEN'
        ORDER BY disagreement DESC
        LIMIT 8;
        """,
        params,
    )
    top_disagree = cur.fetchall()

    # edge_vs_market toplist (requires notes fields)
    cur.execute(
        f"""
        SELECT id, market_id, side, consensus_p_yes, edge, disagreement, reason, notes
        FROM paper_trades
        {where} AND status='OPEN'
        ORDER BY id DESC
        LIMIT 200;
        """,
        params,
    )
    recent_open = cur.fetchall()

    scored: List[Tuple[float, sqlite3.Row, float, float]] = []
    for row in recent_open:
        notes = _safe_json(row["notes"])
        p_yes_mkt, evm, model_yes_re = _extract_market_fields(notes)
        if evm is None:
            continue
        scored.append((abs(evm), row, evm, (p_yes_mkt if p_yes_mkt is not None else float("nan"))))
    scored.sort(key=lambda t: t[0], reverse=True)
    top_vs_market = scored[:8]

    print(f"BLACK GLASS SWARM — PAPER DASHBOARD (Phase 1.6B)")
    print(f"- generated_at_utc: {utc_now_iso()}")
    print(f"- db: {args.db}")
    print(f"- venue: {args.venue}")
    print(f"- include_fake: {bool(args.include_fake)}\n")

    print("TOPLINE")
    print(f"- total_trades:      {total}")
    print(f"- open_trades:       {open_n}")
    print(f"- closed_trades:     {closed_n}")
    print(f"- avg_edge_abs:      {_fmt(avg_edge, 6)}")
    print(f"- avg_disagreement:  {_fmt(avg_disagree, 6)}")
    print(f"- avg_brier_closed:  {_fmt(avg_brier, 6)}")
    print(f"- first_ts:          {first_ts or '-'}")
    print(f"- last_ts:           {last_ts or '-'}\n")

    print("REASON BREAKDOWN")
    if not reasons:
        print("- (none)")
    else:
        for r in reasons:
            print(f"- {r['reason']}: n={r['n']} avg_edge_abs={_fmt(r['avg_edge'])} avg_disagree={_fmt(r['avg_disagree'])}")
    print()

    print("LATEST TRADES (includes market snapshot fields when present)")
    if not latest:
        print("- (none)\n")
    else:
        for r in latest:
            notes = _safe_json(r["notes"])
            p_yes_mkt, evm, _ = _extract_market_fields(notes)
            print(
                f"- id={r['id']} ts={r['ts_utc']} market={r['market_id']} side={r['side']} "
                f"model_yes={_fmt(r['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} "
                f"edge_vs_mkt={_fmt(evm)} edge_abs={_fmt(r['edge'])} disagree={_fmt(r['disagreement'])} "
                f"status={r['status']} outcome={(r['resolved_outcome'] or '-')}"
            )
    print()

    print("OPEN TRADES — TOP EDGE VS MARKET (abs)")
    if not top_vs_market:
        print("- (no snapshot edge_vs_market found yet)\n")
    else:
        for abs_e, row, evm, p_yes_mkt in top_vs_market:
            notes = _safe_json(row["notes"])
            snap = notes.get("snapshot") if isinstance(notes.get("snapshot"), dict) else {}
            updated_at = snap.get("updatedAt")
            print(
                f"- id={row['id']} market={row['market_id']} side={row['side']} "
                f"model_yes={_fmt(row['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} "
                f"edge_vs_mkt={_fmt(evm)} edge_abs={_fmt(row['edge'])} disagree={_fmt(row['disagreement'])} "
                f"updatedAt={updated_at or '-'} reason={row['reason']}"
            )
    print()

    print("OPEN TRADES — TOP EDGE (abs)")
    if not top_edge:
        print("- (none)\n")
    else:
        for r in top_edge:
            notes = _safe_json(r["notes"])
            p_yes_mkt, evm, _ = _extract_market_fields(notes)
            print(
                f"- id={r['id']} market={r['market_id']} side={r['side']} "
                f"model_yes={_fmt(r['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} "
                f"edge_vs_mkt={_fmt(evm)} edge_abs={_fmt(r['edge'])} disagree={_fmt(r['disagreement'])} reason={r['reason']}"
            )
    print()

    print("OPEN TRADES — LOWEST EDGE (abs)")
    if not low_edge:
        print("- (none)\n")
    else:
        for r in low_edge:
            notes = _safe_json(r["notes"])
            p_yes_mkt, evm, _ = _extract_market_fields(notes)
            print(
                f"- id={r['id']} market={r['market_id']} side={r['side']} "
                f"model_yes={_fmt(r['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} "
                f"edge_vs_mkt={_fmt(evm)} edge_abs={_fmt(r['edge'])} disagree={_fmt(r['disagreement'])} reason={r['reason']}"
            )
    print()

    print("OPEN TRADES — HIGHEST DISAGREEMENT")
    if not top_disagree:
        print("- (none)\n")
    else:
        for r in top_disagree:
            notes = _safe_json(r["notes"])
            p_yes_mkt, evm, _ = _extract_market_fields(notes)
            print(
                f"- id={r['id']} market={r['market_id']} side={r['side']} "
                f"model_yes={_fmt(r['consensus_p_yes'])} market_yes={_fmt(p_yes_mkt)} "
                f"edge_vs_mkt={_fmt(evm)} edge_abs={_fmt(r['edge'])} disagree={_fmt(r['disagreement'])} reason={r['reason']}"
            )
    print()

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
