#!/usr/bin/env python3
"""
approve_trades.py — Review and approve/reject pending paper trades.

The infer loop queues trades as PENDING when BGL_REQUIRE_APPROVAL=1.
Run this script to review each one and decide: approve or reject.

Usage: python3 scripts/approve_trades.py
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT    = Path(__file__).parent.parent
DB_PATH = ROOT / "memory" / "runs.sqlite"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main() -> None:
    if not DB_PATH.exists():
        print("DB not found:", DB_PATH)
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    pending = conn.execute(
        "SELECT * FROM paper_trades WHERE status='PENDING' ORDER BY ts_utc ASC"
    ).fetchall()

    if not pending:
        print(f"No pending trades — {now_utc()}")
        conn.close()
        return

    print("═" * 62)
    print(f"  SWARM EDGE — TRADE APPROVAL  {now_utc()}")
    print(f"  {len(pending)} trade(s) awaiting review")
    print("═" * 62)

    approved = rejected = 0

    for r in pending:
        print()
        notes = {}
        try: notes = json.loads(r["notes"] or "{}")
        except: pass

        crowd   = float(notes.get("p_yes_market") or r["p_yes"] or 0.5)
        claude  = float(r["p_yes"] or 0.5)
        edge    = float(r["edge"] or 0)
        side    = (r["side"] or "?").upper()
        stake   = float(r["size_usd"] or 100)
        rationale = (notes.get("llm") or {}).get("rationale", "No rationale stored.")
        category  = notes.get("category", "unknown")
        queued    = r["ts_utc"] or "?"

        p_win = crowd if side == "YES" else (1.0 - crowd)
        p_win = max(0.001, p_win)
        payout = stake / p_win

        print(f"  Market  : {r['question']}")
        print(f"  Side    : {side}   Stake: ${stake:.0f}   Payout if WIN: ${payout:,.0f}")
        print(f"  Crowd   : {crowd:.1%}   Claude: {claude:.1%}   Edge: {edge:.1%}")
        print(f"  Category: {category}   Queued: {queued}")
        print(f"  Rationale: {rationale}")
        print()

        while True:
            ans = input("  Approve? [y]es / [n]o / [s]kip : ").strip().lower()
            if ans in ("y", "yes"):
                notes["approval"] = {
                    "status": "approved",
                    "ts_utc": now_utc(),
                    "by": "approve_trades.py",
                }
                conn.execute(
                    "UPDATE paper_trades SET status='OPEN', notes=? WHERE id=?",
                    (json.dumps(notes, sort_keys=True), r["id"])
                )
                conn.commit()
                print(f"  ✓ APPROVED — trade is now OPEN")
                approved += 1
                break
            elif ans in ("n", "no"):
                reason = input("  Rejection reason (optional): ").strip() or "manual_reject"
                notes["approval"] = {
                    "status": "rejected",
                    "reason": reason,
                    "ts_utc": now_utc(),
                    "by": "approve_trades.py",
                }
                conn.execute(
                    "UPDATE paper_trades SET status='VOID', resolved_outcome=?, notes=? WHERE id=?",
                    (f"REJECTED: {reason}", json.dumps(notes, sort_keys=True), r["id"])
                )
                conn.commit()
                print(f"  ✗ REJECTED — trade voided")
                rejected += 1
                break
            elif ans in ("s", "skip"):
                print("  ~ Skipped — still PENDING")
                break
            else:
                print("  Enter y, n, or s")

    conn.close()
    print()
    print("─" * 62)
    print(f"  Done — {approved} approved  {rejected} rejected")
    print("─" * 62)
    print()


if __name__ == "__main__":
    main()
