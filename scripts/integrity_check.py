#!/usr/bin/env python3
"""
integrity_check.py — Daily system integrity check for Swarm Edge.

Verifies every critical component is working as designed.
Run each morning BEFORE morning_status.py.

Usage: python3 scripts/integrity_check.py
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT      = Path(__file__).parent.parent
DB_PATH   = ROOT / "memory" / "runs.sqlite"
LOG_PATH  = ROOT / "logs" / "infer_loop.log"
DIAG_PATH = ROOT / "signals" / "infer_diagnostics.json"
WATCH_PATH= ROOT / "markets" / "polymarket_watchlist.json"
DATA_DIR  = ROOT / "data"

PASS  = "✓"
WARN  = "~"
FAIL  = "✗"

results: list[tuple[str, str, str]] = []  # (status, label, detail)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def check(status: str, label: str, detail: str = "") -> None:
    results.append((status, label, detail))


# ── 1. KILL SWITCH ────────────────────────────────────────────────────────────
def check_kill():
    if (ROOT / "KILL").exists():
        check(FAIL, "Kill switch", "KILL file present — loop will not run")
    else:
        check(PASS, "Kill switch", "not present")


# ── 2. LOOP RUNNING ───────────────────────────────────────────────────────────
def check_loop():
    try:
        r = subprocess.run(["ps", "aux"], capture_output=True, text=True)
        procs = [l for l in r.stdout.splitlines()
                 if "run_live.sh" in l and "caffeinate" not in l
                 and "integrity" not in l and "grep" not in l]
        if len(procs) == 1:
            pid = procs[0].split()[1]
            check(PASS, "Loop process", f"running (pid {pid})")
        elif len(procs) > 1:
            check(WARN, "Loop process", f"{len(procs)} instances — run pkill -f run_live.sh then restart")
        else:
            check(FAIL, "Loop process", "NOT RUNNING — restart: nohup caffeinate -i bash scripts/run_live.sh >> logs/infer_loop.log 2>&1 &")
    except Exception as e:
        check(WARN, "Loop process", f"could not check: {e}")


# ── 3. LAST RUN RECENCY ───────────────────────────────────────────────────────
def check_last_run():
    try:
        lines = LOG_PATH.read_text().splitlines()
        run_lines = [l for l in lines if "infer loop ==" in l]
        if not run_lines:
            check(FAIL, "Last run", "no cycles logged")
            return
        last = run_lines[-1]
        ts_str = last.split("==")[1].strip().split(" ")[0]
        last_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        mins = int((now_utc() - last_dt).total_seconds() / 60)
        cycles = len(run_lines)
        if mins > 90:
            check(FAIL, "Last run", f"{mins} min ago ({cycles} total cycles) — loop may be stuck")
        elif mins > 70:
            check(WARN, "Last run", f"{mins} min ago ({cycles} total cycles)")
        else:
            check(PASS, "Last run", f"{mins} min ago ({cycles} total cycles)")
    except Exception as e:
        check(WARN, "Last run", f"could not read log: {e}")


# ── 4. ANTHROPIC SDK ──────────────────────────────────────────────────────────
def check_sdk():
    try:
        import anthropic  # noqa
        check(PASS, "Anthropic SDK", f"installed (v{anthropic.__version__})")
    except ImportError:
        check(FAIL, "Anthropic SDK", "NOT INSTALLED — run: pip install anthropic")


# ── 5. API KEY + claude_enabled() ────────────────────────────────────────────
def check_api_key():
    # Load .env manually
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        check(FAIL, "API key", "ANTHROPIC_API_KEY not set in .env")
        return

    try:
        from llm.claude_client import claude_enabled
        if claude_enabled():
            check(PASS, "API key", f"set ({key[:8]}...) — claude_enabled: True")
        else:
            check(FAIL, "API key", "key present but claude_enabled() is False — SDK missing?")
    except Exception as e:
        check(WARN, "API key", f"key set but could not verify claude_enabled: {e}")


# ── 6. LLM ACTUALLY BEING CALLED ─────────────────────────────────────────────
def check_llm_usage():
    try:
        d = json.loads(DIAG_PATH.read_text())
        ts = d.get("ts_utc", "")
        rows = d.get("rows", [])

        # Check freshness
        try:
            diag_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            hours_old = (now_utc() - diag_dt).total_seconds() / 3600
        except Exception:
            hours_old = 999

        if hours_old > 3:
            check(WARN, "Diagnostics age", f"{hours_old:.0f}h old — export may be stale")
        else:
            check(PASS, "Diagnostics age", f"{hours_old:.1f}h old")

        # Check LLM usage
        llm_called   = [r for r in rows if r.get("llm_used") is True]
        llm_skipped  = [r for r in rows if r.get("llm_used") is False]
        llm_none     = [r for r in rows if r.get("llm_used") is None]

        if llm_called:
            check(PASS, "LLM (Claude) called", f"{len(llm_called)}/{len(rows)} markets in last batch")
        elif llm_skipped:
            check(FAIL, "LLM (Claude) called",
                  f"0/{len(rows)} — Claude not being used. Check SDK + API key + BGL_INFER_USE_LLM=1")
        else:
            # All None = markets rejected before reaching LLM (wide_spread etc)
            reasons = [r.get("reason","?") for r in llm_none]
            check(WARN, "LLM (Claude) called",
                  f"all {len(rows)} markets rejected pre-LLM ({', '.join(set(reasons))})")

    except FileNotFoundError:
        check(FAIL, "Diagnostics", "signals/infer_diagnostics.json not found")
    except Exception as e:
        check(WARN, "Diagnostics", f"could not parse: {e}")


# ── 7. WATCHLIST HEALTH ───────────────────────────────────────────────────────
SPORTS_PATTERNS = [
    r"nba finals", r"stanley cup", r"super bowl", r"world series",
    r"nhl playoff", r"nba playoff", r"nfl playoff", r"make the playoff",
    r"fifa world cup", r"premier league", r"champions league",
]

def check_watchlist():
    try:
        wl = json.loads(WATCH_PATH.read_text())
        count = len(wl)

        # Check for sports that slipped through
        sports = [e["market_id"] for e in wl
                  if any(re.search(p, e["market_id"].lower()) for p in SPORTS_PATTERNS)]

        if count == 0:
            check(FAIL, "Watchlist", "empty — run: python3 scripts/manage_watchlist.py --apply")
        elif sports:
            check(WARN, "Watchlist", f"{count} markets but {len(sports)} sports market(s) detected: {sports}")
        elif count < 10:
            check(WARN, "Watchlist", f"only {count} markets — may need refresh")
        else:
            check(PASS, "Watchlist", f"{count} markets")
    except FileNotFoundError:
        check(FAIL, "Watchlist", "markets/polymarket_watchlist.json not found")
    except Exception as e:
        check(WARN, "Watchlist", f"could not read: {e}")


# ── 8. DATABASE ───────────────────────────────────────────────────────────────
def check_database():
    if not DB_PATH.exists():
        check(FAIL, "Database", f"{DB_PATH} not found")
        return
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT status, COUNT(*) as n FROM paper_trades GROUP BY status").fetchall()
        conn.close()
        summary = {r["status"]: r["n"] for r in rows}
        total = sum(summary.values())
        if total == 0:
            check(WARN, "Database", "paper_trades table is empty")
        else:
            parts = [f"{v} {k}" for k, v in summary.items()]
            check(PASS, "Database", f"{total} trades ({', '.join(parts)})")
    except Exception as e:
        check(FAIL, "Database", f"error: {e}")


# ── 9. POSITIONS SANITY ───────────────────────────────────────────────────────
def check_positions():
    jfile = DATA_DIR / "paper_trades.json"
    if not jfile.exists():
        check(WARN, "Positions data", "data/paper_trades.json not found — run export_data.py")
        return
    try:
        trades = json.loads(jfile.read_text())
        open_t = [t for t in trades if t.get("status") == "OPEN"]
        nan_trades = []
        bad_crowd  = []
        for t in open_t:
            notes = {}
            try: notes = json.loads(t.get("notes") or "{}")
            except: pass
            crowd = notes.get("p_yes_market") or notes.get("crowd_p_yes")
            try:
                c = float(crowd)
                if math.isnan(c) or c <= 0:
                    nan_trades.append(t.get("market_id","?"))
            except:
                bad_crowd.append(t.get("market_id","?"))

        if nan_trades or bad_crowd:
            check(WARN, "Positions sanity", f"bad crowd prices: {nan_trades + bad_crowd}")
        else:
            total_stake = sum(float(t.get("size_usd",0)) for t in open_t)
            check(PASS, "Positions sanity", f"{len(open_t)} open, ${total_stake:.0f} deployed, no NaN prices")
    except Exception as e:
        check(WARN, "Positions sanity", f"could not parse: {e}")


# ── 10. EXPORT FRESHNESS ──────────────────────────────────────────────────────
def check_export():
    jfile = DATA_DIR / "paper_trades.json"
    if not jfile.exists():
        check(WARN, "Export freshness", "data/paper_trades.json missing")
        return
    try:
        mtime = datetime.fromtimestamp(jfile.stat().st_mtime, tz=timezone.utc)
        hours = (now_utc() - mtime).total_seconds() / 3600
        if hours > 12:
            check(WARN, "Export freshness", f"{hours:.0f}h since last export — dashboard may be stale")
        else:
            check(PASS, "Export freshness", f"last exported {hours:.1f}h ago")
    except Exception as e:
        check(WARN, "Export freshness", f"could not check: {e}")


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    ts = now_utc().strftime("%Y-%m-%d  %H:%M UTC")
    width = 60
    print("═" * width)
    print(f"  SWARM EDGE — INTEGRITY CHECK  {ts}")
    print("═" * width)
    print()

    check_kill()
    check_loop()
    check_last_run()
    check_sdk()
    check_api_key()
    check_llm_usage()
    check_watchlist()
    check_database()
    check_positions()
    check_export()

    print()
    fails  = [r for r in results if r[0] == FAIL]
    warns  = [r for r in results if r[0] == WARN]
    passes = [r for r in results if r[0] == PASS]

    for status, label, detail in results:
        detail_str = f"  {detail}" if detail else ""
        print(f"  {status}  {label:<28}{detail_str}")

    print()
    if fails:
        print(f"  {len(fails)} CRITICAL  {len(warns)} warnings  {len(passes)} passed")
        print()
        print("  ACTION REQUIRED:")
        for _, label, detail in fails:
            print(f"    • {label}: {detail}")
    elif warns:
        print(f"  {len(warns)} warnings  {len(passes)} passed — review above")
    else:
        print(f"  All {len(passes)} checks passed — system healthy")

    print()
    print("─" * width)
    print()


if __name__ == "__main__":
    main()
