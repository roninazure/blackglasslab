#!/usr/bin/env python3
"""
void_trades.py — Manually void specific paper trades by market_id slug.

Usage:
    python3 scripts/void_trades.py                  # dry run — shows what would be voided
    python3 scripts/void_trades.py --apply          # actually void them
"""
from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

ROOT    = Path(__file__).parent.parent
DB_PATH = ROOT / "memory" / "runs.sqlite"

# Trades to void — bad entries from before exclusion filters were tightened
VOID_SLUGS = [
    "will-nottm-forest-win-the-2025-26-uefa",       # Europa League — sports
    "will-freiburg-win-the-2025-26-uefa-eur",        # Europa League — sports
    "will-aston-villa-win-the-2025-26-uefa-",        # Europa League — sports
    "will-there-be-no-change-in-fed-interes",        # extreme tail — 99% crowd
    "will-5-fed-rate-cuts-happen-in-2026",           # extreme tail — 1.1% crowd
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="Actually void the trades")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    print(f"VOID TRADES — {'APPLY' if args.apply else 'DRY RUN'}")
    print()

    voided = 0
    for slug in VOID_SLUGS:
        # Match on partial slug (DB stores full slug, our list may be truncated)
        rows = conn.execute(
            "SELECT id, market_id, side, size_usd, status FROM paper_trades "
            "WHERE market_id LIKE ? AND status = 'OPEN'",
            (f"{slug}%",)
        ).fetchall()

        if not rows:
            print(f"  -- not found / already closed: {slug}")
            continue

        for r in rows:
            print(f"  VOID  {r['market_id']}  {r['side']}  ${r['size_usd']:.0f}")
            if args.apply:
                conn.execute(
                    "UPDATE paper_trades SET status='VOID', resolved_outcome='VOID' WHERE id=?",
                    (r["id"],)
                )
                voided += 1

    if args.apply:
        conn.commit()
        print()
        print(f"Voided {voided} trade(s).")
    else:
        print()
        print("Dry run — no changes made. Run with --apply to void.")

    conn.close()


if __name__ == "__main__":
    main()
