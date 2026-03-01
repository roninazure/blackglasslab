#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, List, Optional, Tuple

DEFAULT_DB = os.path.join("memory", "runs.sqlite")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _fetch_one(conn: sqlite3.Connection, q: str, params: Tuple[Any, ...] = ()) -> sqlite3.Row:
    cur = conn.execute(q, params)
    row = cur.fetchone()
    if row is None:
        raise RuntimeError("query returned no rows")
    return row


def _fetch_all(conn: sqlite3.Connection, q: str, params: Tuple[Any, ...] = ()) -> List[sqlite3.Row]:
    return list(conn.execute(q, params).fetchall())


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _notes_market_yes(notes: Any) -> Optional[float]:
    if not isinstance(notes, str) or not notes.strip():
        return None
    try:
        obj = json.loads(notes)
        return _safe_float(obj.get("p_yes_market"))
    except Exception:
        return None


def fmt(n: Any, nd: int = 4) -> str:
    if n is None:
        return "-"
    if isinstance(n, int):
        return str(n)
    try:
        f = float(n)
        return f"{f:.{nd}f}"
    except Exception:
        return str(n)


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 1.5/1.6: Paper trading dashboard (SQLite)")
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--venue", default="polymarket")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--include-fake", action="store_true")
    args = ap.parse_args()

    venue = (args.venue or "").strip() or "polymarket"
    conn = _connect_db(args.db)

    fake_filter = "" if args.include_fake else "AND market_id NOT LIKE 'FAKE-%'"

    topline = _fetch_one(
        conn,
        f"""
        SELECT
          COUNT(*)                                                AS total_trades,
          SUM(CASE WHEN status='OPEN'   THEN 1 ELSE 0 END)       AS open_trades,
          SUM(CASE WHEN status='CLOSED' THEN 1 ELSE 0 END)       AS closed_trades,
          ROUND(AVG(edge), 6)                                     AS avg_edge,
          ROUND(AVG(disagreement), 6)                             AS avg_disagreement,
          ROUND(AVG(CASE WHEN status='CLOSED' THEN brier END), 6) AS avg_brier_closed,
          MIN(ts_utc)                                             AS first_ts,
          MAX(ts_utc)                                             AS last_ts
        FROM paper_trades
        WHERE venue=?
          {fake_filter}
        """,
        (venue,),
    )

    reasons = _fetch_all(
        conn,
        f"""
        SELECT reason,
               COUNT(*) AS n,
               ROUND(AVG(edge), 6) AS avg_edge,
               ROUND(AVG(disagreement), 6) AS avg_disagree
        FROM paper_trades
        WHERE venue=?
          {fake_filter}
        GROUP BY reason
        ORDER BY n DESC, reason ASC
        """,
        (venue,),
    )

    latest = _fetch_all(
        conn,
        f"""
        SELECT id, ts_utc, market_id, side, consensus_p_yes, edge, disagreement, status, resolved_outcome, brier, notes
        FROM paper_trades
        WHERE venue=?
          {fake_filter}
        ORDER BY id DESC
        LIMIT ?;
        """,
        (venue, int(args.limit)),
    )

    best_edge = _fetch_all(
        conn,
        f"""
        SELECT id, market_id, side, consensus_p_yes, edge, disagreement, reason, notes
        FROM paper_trades
        WHERE venue=? AND status='OPEN'
          {fake_filter}
        ORDER BY edge DESC, id DESC
        LIMIT 5;
        """,
        (venue,),
    )

    conn.close()

    print("BLACK GLASS SWARM — PAPER DASHBOARD (Phase 1.6A)")
    print(f"- generated_at_utc: {utc_now_iso()}")
    print(f"- db: {args.db}")
    print(f"- venue: {venue}")
    print(f"- include_fake: {bool(args.include_fake)}")
    print()

    print("TOPLINE")
    print(f"- total_trades:      {topline['total_trades']}")
    print(f"- open_trades:       {topline['open_trades']}")
    print(f"- closed_trades:     {topline['closed_trades']}")
    print(f"- avg_edge:          {fmt(topline['avg_edge'], 6)}")
    print(f"- avg_disagreement:  {fmt(topline['avg_disagreement'], 6)}")
    print(f"- avg_brier_closed:  {fmt(topline['avg_brier_closed'], 6)}")
    print(f"- first_ts:          {topline['first_ts'] or '-'}")
    print(f"- last_ts:           {topline['last_ts'] or '-'}")
    print()

    print("REASON BREAKDOWN")
    if not reasons:
        print("- (none)")
    else:
        for r in reasons:
            print(f"- {r['reason']}: n={r['n']} avg_edge={fmt(r['avg_edge'],6)} avg_disagree={fmt(r['avg_disagree'],6)}")
    print()

    print("LATEST TRADES")
    if not latest:
        print("- (none)")
    else:
        for r in latest:
            mkt_yes = _notes_market_yes(r["notes"])
            ro = r["resolved_outcome"] if r["resolved_outcome"] else "-"
            print(
                f"- id={r['id']} ts={r['ts_utc']} market={r['market_id']} side={r['side']} "
                f"market_yes={fmt(mkt_yes,6)} model_yes={fmt(r['consensus_p_yes'],6)} "
                f"edge={fmt(r['edge'],6)} disagree={fmt(r['disagreement'],6)} "
                f"status={r['status']} outcome={ro} brier={fmt(r['brier'],6)}"
            )
    print()

    print("OPEN TRADES — TOP EDGE (with market_yes)")
    for r in best_edge:
        mkt_yes = _notes_market_yes(r["notes"])
        print(
            f"- id={r['id']} market={r['market_id']} side={r['side']} "
            f"market_yes={fmt(mkt_yes,6)} model_yes={fmt(r['consensus_p_yes'],6)} "
            f"edge={fmt(r['edge'],6)} disagree={fmt(r['disagreement'],6)} reason={r['reason']}"
        )
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
