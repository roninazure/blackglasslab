#!/usr/bin/env python3
"""
export_data.py — Export live data to JSON for Streamlit Cloud dashboard.

Dumps open trades from SQLite and copies infer_diagnostics.json.
Git publication is opt-in via SWARM_EDGE_PUBLISH_ENABLED=1.

Called automatically by run_live.sh every 6 cycles (~6h).
Run manually: python3 scripts/export_data.py
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from swarm_edge_runtime import RUNTIME_PATHS

ROOT     = RUNTIME_PATHS.root
DB_PATH  = RUNTIME_PATHS.db_path
DATA_DIR = RUNTIME_PATHS.data_dir
TRADES_OUT  = DATA_DIR / "paper_trades.json"
DIAG_SRC    = RUNTIME_PATHS.signals_dir / "infer_diagnostics.json"
DIAG_OUT    = DATA_DIR / "infer_diagnostics.json"
CUTOFF      = "2026-03-28T21:00"
PUBLISH_ENABLED = os.getenv("SWARM_EDGE_PUBLISH_ENABLED", "0").strip() in {"1", "true", "TRUE", "yes", "YES"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def utc_now_label() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def now_utc() -> str:
    """Return a commit-safe UTC timestamp for optional publication."""
    return utc_now_iso()


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
    payload = {
        "generated_at_utc": utc_now_iso(),
        "source_db_path": str(DB_PATH),
        "records": records,
    }
    DATA_DIR.mkdir(exist_ok=True)
    TRADES_OUT.write_text(json.dumps(payload, indent=2, default=str))
    print(f"  [export] {len(records)} open trades → data/paper_trades.json")
    return len(records)


def export_diagnostics() -> bool:
    if not DIAG_SRC.exists():
        print("  [export] diagnostics not found — skipping")
        return False
    DATA_DIR.mkdir(exist_ok=True)
    diag = json.loads(DIAG_SRC.read_text())
    if isinstance(diag, dict):
        diag["generated_at_utc"] = utc_now_iso()
        diag["source_db_path"] = str(DB_PATH)
    DIAG_OUT.write_text(json.dumps(diag, indent=2, sort_keys=True, default=str))
    print("  [export] diagnostics → data/infer_diagnostics.json")
    return True


def git_push() -> None:
    if not PUBLISH_ENABLED:
        print("  [export] publication disabled (SWARM_EDGE_PUBLISH_ENABLED=0)")
        return
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
    commit_status = run(["git", "commit", "-m", f"data: snapshot {ts}"])
    if commit_status != 0:
        # A failed publication must not leave generated files staged.
        run(["git", "reset", "--quiet", "--"] + files)
        print("  [export] commit failed — generated files unstaged")
        return

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
    print(f"EXPORT — {utc_now_label()}")
    try:
        export_trades()
        export_diagnostics()
    except Exception as exc:
        print(f"  [export] local export failed: {exc}")
    try:
        git_push()
    except Exception as exc:
        print(f"  [export] publication failed: {exc}")


if __name__ == "__main__":
    main()
