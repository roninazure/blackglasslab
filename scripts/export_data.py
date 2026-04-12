#!/usr/bin/env python3
"""
export_data.py — Export live data to JSON for Streamlit Cloud dashboard.

Dumps open trades from SQLite and copies infer_diagnostics.json,
then commits and pushes so Streamlit Cloud picks up the latest snapshot.

Called automatically by run_live.sh every 6 cycles (~6h).
Run manually: python3 scripts/export_data.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT     = Path(__file__).parent.parent
DB_PATH  = ROOT / "memory" / "runs.sqlite"
DATA_DIR = ROOT / "data"
TRADES_OUT  = DATA_DIR / "paper_trades.json"
DIAG_SRC    = ROOT / "signals" / "infer_diagnostics.json"
DIAG_OUT    = DATA_DIR / "infer_diagnostics.json"
CUTOFF      = "2026-03-28T21:00"


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def export_trades() -> int:
    if not DB_PATH.exists():
        print("  [export] DB not found — skipping")
        return 0
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        f"SELECT * FROM paper_trades "
        f"WHERE ts_utc > '{CUTOFF}' AND status = 'OPEN' "
        f"ORDER BY ts_utc DESC"
    ).fetchall()
    conn.close()
    records = [dict(r) for r in rows]
    DATA_DIR.mkdir(exist_ok=True)
    TRADES_OUT.write_text(json.dumps(records, indent=2, default=str))
    print(f"  [export] {len(records)} open trades → data/paper_trades.json")
    return len(records)


def export_diagnostics() -> bool:
    if not DIAG_SRC.exists():
        print("  [export] diagnostics not found — skipping")
        return False
    DATA_DIR.mkdir(exist_ok=True)
    DIAG_OUT.write_text(DIAG_SRC.read_text())
    print("  [export] diagnostics → data/infer_diagnostics.json")
    return True


def git_push() -> None:
    files = []
    if TRADES_OUT.exists():
        files.append("data/paper_trades.json")
    if DIAG_OUT.exists():
        files.append("data/infer_diagnostics.json")
    if not files:
        print("  [export] nothing to commit")
        return

    def run(cmd: list[str]) -> int:
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        return r.returncode

    run(["git", "add"] + files)

    # Check if anything actually changed
    if run(["git", "diff", "--cached", "--quiet"]) == 0:
        print("  [export] no changes — skipping push")
        return

    ts = now_utc()
    run(["git", "commit", "-m", f"data: snapshot {ts}"])

    # Pull rebase first to avoid conflicts, then push
    pull = subprocess.run(
        ["git", "pull", "--rebase", "--autostash"],
        cwd=ROOT, capture_output=True, text=True
    )
    if pull.returncode != 0:
        print(f"  [export] pull failed — skipping push: {pull.stderr.strip()}")
        return

    push = subprocess.run(
        ["git", "push"],
        cwd=ROOT, capture_output=True, text=True
    )
    if push.returncode == 0:
        print(f"  [export] pushed snapshot — {ts}")
    else:
        print(f"  [export] push failed: {push.stderr.strip()}")


def main() -> None:
    print(f"EXPORT — {now_utc()}")
    export_trades()
    export_diagnostics()
    git_push()


if __name__ == "__main__":
    main()
