#!/usr/bin/env python3
"""
morning_status.py — Swarm Edge daily briefing.
Run each morning: python3 scripts/morning_status.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
DB_PATH = ROOT / "memory" / "runs.sqlite"
LOG_PATH = ROOT / "logs" / "infer_loop.log"
DIAG_PATH = ROOT / "signals" / "infer_diagnostics.json"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def divider(char="═", width=58):
    print(char * width)


def header():
    divider()
    ts = now_utc().strftime("%Y-%m-%d  %H:%M UTC")
    print(f"  SWARM EDGE — MORNING BRIEFING  {ts}")
    divider()


def check_loop():
    print()
    print("LOOP")
    try:
        result = subprocess.run(
            ["pgrep", "-af", "run_live.sh"],
            capture_output=True, text=True
        )
        pids = [l for l in result.stdout.strip().splitlines() if "run_live" in l]
        if pids:
            pid = pids[0].split()[0]
            print(f"  status   RUNNING  (pid {pid})")
            if len(pids) > 1:
                print(f"  WARNING  {len(pids)} instances running — kill extras with: pkill -f run_live.sh && nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &")
        else:
            print("  status   NOT RUNNING  ← restart: nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &")
    except Exception:
        print("  status   UNKNOWN")

    # Last run time from log
    try:
        lines = LOG_PATH.read_text().splitlines()
        run_lines = [l for l in lines if "infer loop ==" in l]
        if run_lines:
            last = run_lines[-1]
            ts_str = last.split("==")[1].strip().split(" ")[0]
            last_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            delta = now_utc() - last_dt
            mins = int(delta.total_seconds() / 60)
            print(f"  last run {ts_str}  ({mins} min ago)")
            print(f"  cycles   {len(run_lines)} total")
        else:
            print("  last run unknown")
    except Exception:
        print("  last run unknown")


def check_positions():
    print()
    if not DB_PATH.exists():
        print("POSITIONS  db not found")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        "SELECT * FROM paper_trades ORDER BY ts_utc DESC"
    ).fetchall()

    open_trades = [r for r in rows if r["status"] == "OPEN"]
    closed = [r for r in rows if r["status"] == "CLOSED"]
    void = [r for r in rows if r["status"] == "VOID"]

    print(f"POSITIONS  [{len(open_trades)} open  {len(closed)} closed  {len(void)} void]")

    for r in open_trades:
        slug = (r["market_id"] or "")[:38]
        side = r["side"] or "?"
        edge = float(r["edge"] or 0)
        ts = r["ts_utc"] or ""
        try:
            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            days_held = (now_utc() - entry_dt).days
        except Exception:
            days_held = "?"
        print(f"  {slug:<38}  {side:<3}  edge={edge:.3f}  held {days_held}d")

    if closed:
        print()
        print(f"CLOSED TRADES  [{len(closed)}]")
        total_profit = 0.0
        for r in closed:
            slug = (r["market_id"] or "")[:38]
            outcome = r["resolved_outcome"] or "?"
            brier = r["brier"]
            notes = {}
            try:
                notes = json.loads(r["notes"] or "{}")
            except Exception:
                pass
            profit = notes.get("profit_usd") or (notes.get("resolution") or {}).get("profit_usd") or 0
            total_profit += float(profit)
            brier_s = f"{brier:.4f}" if brier is not None else "    ?"
            print(f"  {slug:<38}  {outcome:<3}  brier={brier_s}  profit=${float(profit):+.2f}")
        print(f"  {'TOTAL P&L':<38}                          ${total_profit:+.2f}")

    conn.close()


def check_last_eval():
    print()
    print("LAST EVALUATION")
    try:
        d = json.loads(DIAG_PATH.read_text())
        ts = d.get("ts_utc", "unknown")
        print(f"  run: {ts}")
        rows = d.get("rows", [])
        if not rows:
            print("  no markets evaluated")
        for r in rows:
            slug = (r.get("slug") or "")[:38]
            decision = r.get("decision", "?")
            edge = r.get("edge_abs", 0)
            reason = r.get("reason", "")
            print(f"  {slug:<38}  {decision:<7}  edge={edge:.3f}  {reason}")
    except Exception as e:
        print(f"  could not load diagnostics: {e}")


def check_api_cost():
    print()
    print("API COST ESTIMATE")
    try:
        lines = LOG_PATH.read_text().splitlines()
        cycles = len([l for l in lines if "infer loop ==" in l])
        # ~5 Claude Haiku calls per cycle avg, ~$0.000025 per call
        cost = cycles * 5 * 0.000025
        print(f"  cycles run     {cycles}")
        print(f"  est. API cost  ${cost:.4f}  (~${cost*30/max(cycles,1):.2f}/month at this rate)")
    except Exception:
        print("  unable to estimate")


def footer():
    print()
    divider("─")
    print("  next steps:")
    print("  • resolve trades:  python3 scripts/resolve_paper_trades.py")
    print("  • full P&L:        python3 scripts/watch_resolutions.py")
    print("  • restart loop:    pkill -f run_live.sh && nohup bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &")
    divider("─")
    print()


def main():
    header()
    check_loop()
    check_positions()
    check_last_eval()
    check_api_cost()
    footer()


if __name__ == "__main__":
    main()
