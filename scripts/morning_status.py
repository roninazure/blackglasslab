#!/usr/bin/env python3
"""
morning_status.py — Swarm Edge daily briefing + integrity check.
Run each morning: python3 scripts/morning_status.py
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT      = Path(__file__).parent.parent
DB_PATH   = ROOT / "memory" / "runs.sqlite"
LOG_PATH  = ROOT / "logs" / "infer_loop.log"
DIAG_PATH = ROOT / "signals" / "infer_diagnostics.json"
WATCH_PATH= ROOT / "markets" / "polymarket_watchlist.json"
DATA_DIR  = ROOT / "data"

PASS = "✓"
WARN = "~"
FAIL = "✗"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def divider(char="═", width=58):
    print(char * width)


def header():
    divider()
    ts = now_utc().strftime("%Y-%m-%d  %H:%M UTC")
    print(f"  SWARM EDGE — MORNING BRIEFING  {ts}")
    divider()


# ── SYSTEM HEALTH ─────────────────────────────────────────────────────────────

def check_health():
    checks: list[tuple[str, str, str]] = []

    # SDK installed
    try:
        import anthropic
        checks.append((PASS, "SDK", f"anthropic {anthropic.__version__}"))
    except ImportError:
        checks.append((FAIL, "SDK", "MISSING — pip install anthropic"))

    # API key + claude_enabled
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    try:
        from llm.claude_client import claude_enabled
        if claude_enabled():
            checks.append((PASS, "Claude", "enabled"))
        else:
            checks.append((FAIL, "Claude", "claude_enabled()=False — SDK or key missing"))
    except Exception as e:
        checks.append((WARN, "Claude", f"could not verify: {e}"))

    # LLM actually called in last batch
    try:
        d = json.loads(DIAG_PATH.read_text())
        rows = d.get("rows", [])
        diag_ts = d.get("ts_utc", "")
        try:
            diag_dt = datetime.fromisoformat(diag_ts.replace("Z", "+00:00"))
            hours_old = (now_utc() - diag_dt).total_seconds() / 3600
            if hours_old > 3:
                checks.append((WARN, "Diagnostics", f"{hours_old:.0f}h stale"))
            else:
                pass  # fresh, no need to surface
        except Exception:
            pass
        llm_used = [r for r in rows if r.get("llm_used") is True]
        llm_off  = [r for r in rows if r.get("llm_used") is False]
        if llm_used:
            checks.append((PASS, "LLM calls", f"{len(llm_used)}/{len(rows)} in last batch"))
        elif llm_off:
            checks.append((FAIL, "LLM calls", "0 — Claude not being called, check SDK+key"))
        # all None = rejected pre-LLM, not a failure
    except FileNotFoundError:
        checks.append((WARN, "LLM calls", "no diagnostics yet"))
    except Exception as e:
        checks.append((WARN, "LLM calls", f"could not check: {e}"))

    # Watchlist sports leak
    try:
        wl = json.loads(WATCH_PATH.read_text())
        sports_patterns = [
            r"nba.finals", r"stanley.cup", r"super.bowl", r"world.series",
            r"nhl.playoff", r"nba.playoff", r"make.the.playoff",
        ]
        sports = [e["market_id"] for e in wl
                  if any(re.search(p, e["market_id"].lower()) for p in sports_patterns)]
        if sports:
            checks.append((WARN, "Watchlist", f"{len(sports)} sports market(s) leaked in"))
        else:
            checks.append((PASS, "Watchlist", f"{len(wl)} markets, clean"))
    except Exception:
        checks.append((WARN, "Watchlist", "could not read"))

    # Print health section
    print()
    print("SYSTEM HEALTH")
    for status, label, detail in checks:
        print(f"  {status}  {label:<14}  {detail}")

    # Surface any failures prominently
    failures = [c for c in checks if c[0] == FAIL]
    if failures:
        print()
        print("  !! ACTION REQUIRED !!")
        for _, label, detail in failures:
            print(f"     {label}: {detail}")


def check_loop():
    print()
    print("LOOP")
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True, text=True
        )
        # Only count bash/sh processes — caffeinate wrapping run_live.sh
        # creates a parent process that also matches, causing false duplicates
        pids = [l for l in result.stdout.strip().splitlines()
                if "run_live.sh" in l
                and "morning_status" not in l
                and "grep" not in l
                and "caffeinate" not in l]
        if len(pids) == 1:
            pid = pids[0].split()[1]
            print(f"  status   RUNNING  (pid {pid})")
        elif len(pids) > 1:
            pid_list = " ".join(l.split()[1] for l in pids)
            print(f"  WARNING  {len(pids)} instances running (pids {pid_list})")
            print(f"           fix: pkill -f run_live.sh && pkill -f live_runner.py && nohup caffeinate -i bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &")
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

    total_exposure = sum(float(r["size_usd"] or 100) for r in open_trades)

    print(f"POSITIONS  [{len(open_trades)} open  {len(closed)} closed  {len(void)} void]")
    print(f"  {'MARKET':<38}  {'SIDE':<3}  {'CROWD':<6}  {'BET':>6}  {'WIN PAYOUT':>11}  {'EDGE':>6}  HELD")

    total_win = 0.0
    for r in open_trades:
        slug = (r["market_id"] or "")[:38]
        side = (r["side"] or "?").upper()
        edge = float(r["edge"] or 0)
        size = float(r["size_usd"] or 100)
        p_yes_claude = float(r["p_yes"] or 0.5)
        ts = r["ts_utc"] or ""
        try:
            entry_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            days_held = (now_utc() - entry_dt).days
        except Exception:
            days_held = "?"

        # Crowd price from notes (same source as dashboard)
        notes = {}
        try:
            notes = json.loads(r["notes"] or "{}")
        except Exception:
            pass
        crowd_raw = notes.get("p_yes_market") or notes.get("crowd_p_yes")
        try:
            crowd_p_yes = float(crowd_raw) if crowd_raw is not None else p_yes_claude
            if crowd_p_yes != crowd_p_yes:  # NaN check
                crowd_p_yes = p_yes_claude
        except Exception:
            crowd_p_yes = p_yes_claude

        # Win payout = total return (stake ÷ crowd price), matching dashboard
        try:
            p_win = crowd_p_yes if side == "YES" else (1.0 - crowd_p_yes)
            p_win = max(0.001, min(0.999, p_win))
            win_payout = round(size / p_win, 2)
        except Exception:
            win_payout = 0.0
        total_win += win_payout

        print(f"  {slug:<38}  {side:<3}  {crowd_p_yes:>5.1%}  ${size:>5.0f}  ${win_payout:>10.2f}  {edge:>5.1%}  {days_held}d")

    print()
    print(f"  {'TOTAL EXPOSURE':<38}                 ${total_exposure:>6.0f}")
    print(f"  {'TOTAL IF ALL WIN':<38}                        ${total_win:>10.2f}")
    print(f"  {'PROFIT IF ALL WIN':<38}                        ${total_win - total_exposure:>+10.2f}")

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
    print("  • restart loop:    pkill -f run_live.sh && pkill -f live_runner.py && nohup caffeinate -i bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &")
    divider("─")
    print()


def main():
    header()
    check_health()
    check_loop()
    check_positions()
    check_last_eval()
    check_api_cost()
    footer()


if __name__ == "__main__":
    main()
