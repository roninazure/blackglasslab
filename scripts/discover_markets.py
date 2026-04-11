#!/usr/bin/env python3
"""
discover_markets.py — Find near-term Polymarket markets worth tracking.

Browses the Gamma API by pagination (not search) to find active markets
resolving within a target window. Prints candidates sorted by days to close.

Usage:
    python3 scripts/discover_markets.py
    python3 scripts/discover_markets.py --days 30 --min-volume 10000
    python3 scripts/discover_markets.py --days 60 --add   # auto-add to watchlist
"""
from __future__ import annotations

import argparse
import json
import time
import urllib.request
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
WATCHLIST_PATH = ROOT / "markets" / "polymarket_watchlist.json"
GAMMA_BASE = "https://gamma-api.polymarket.com"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://polymarket.com/",
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_page(offset: int, limit: int = 100) -> list:
    qs = urllib.parse.urlencode({
        "limit": limit,
        "offset": offset,
        "active": "true",
        "closed": "false",
    })
    url = f"{GAMMA_BASE}/markets?{qs}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [fetch error offset={offset}] {e}")
        return []


def parse_end_date(m: dict) -> datetime | None:
    for field in ["endDate", "endDateIso", "end_date"]:
        val = m.get(field)
        if val:
            try:
                return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            except Exception:
                continue
    return None


def load_watchlist() -> list:
    if not WATCHLIST_PATH.exists():
        return []
    return json.loads(WATCHLIST_PATH.read_text())


def save_watchlist(markets: list) -> None:
    WATCHLIST_PATH.write_text(json.dumps(markets, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Discover near-term Polymarket markets")
    ap.add_argument("--days", type=int, default=45, help="Max days to resolution (default 45)")
    ap.add_argument("--min-days", type=int, default=2, help="Min days to resolution (default 2)")
    ap.add_argument("--min-volume", type=float, default=5000, help="Min total volume USD (default 5000)")
    ap.add_argument("--pages", type=int, default=10, help="Pages to fetch (100 markets each)")
    ap.add_argument("--add", action="store_true", help="Auto-add candidates to watchlist")
    args = ap.parse_args()

    existing = {m["market_id"] for m in load_watchlist()}
    candidates = []
    now = now_utc()

    print(f"Scanning Polymarket for markets resolving in {args.min_days}–{args.days} days...")
    print(f"Min volume: ${args.min_volume:,.0f}")
    print()

    for page in range(args.pages):
        offset = page * 100
        markets = fetch_page(offset)
        if not markets:
            break

        for m in markets:
            if not m.get("active") or m.get("closed"):
                continue

            end_dt = parse_end_date(m)
            if not end_dt:
                continue

            days = (end_dt - now).days
            if not (args.min_days <= days <= args.days):
                continue

            volume = float(m.get("volume") or 0)
            if volume < args.min_volume:
                continue

            slug = m.get("slug", "")
            if not slug:
                continue

            candidates.append({
                "days": days,
                "slug": slug,
                "question": m.get("question", "")[:80],
                "volume": volume,
                "end_date": end_dt.strftime("%Y-%m-%d"),
                "in_watchlist": slug in existing,
            })

        time.sleep(0.1)

    candidates.sort(key=lambda x: x["days"])

    if not candidates:
        print("No candidates found in this window.")
        print("Try: --days 90 --min-volume 1000")
        return

    new = [c for c in candidates if not c["in_watchlist"]]
    already = [c for c in candidates if c["in_watchlist"]]

    print(f"Found {len(candidates)} candidates ({len(new)} new, {len(already)} already in watchlist)")
    print()

    if already:
        print("ALREADY IN WATCHLIST:")
        for c in already:
            print(f"  ✓ {c['days']:3}d  ${c['volume']:>10,.0f}  {c['slug'][:55]}")
        print()

    if new:
        print("NEW CANDIDATES:")
        for c in new:
            print(f"  + {c['days']:3}d  ${c['volume']:>10,.0f}  {c['slug'][:55]}")
            print(f"           {c['question']}")
        print()

    if args.add and new:
        watchlist = load_watchlist()
        added = []
        for c in new:
            watchlist.append({"market_id": c["slug"]})
            added.append(c["slug"])
        save_watchlist(watchlist)
        print(f"Added {len(added)} markets to watchlist:")
        for s in added:
            print(f"  + {s}")
    elif new and not args.add:
        print("To add these to the watchlist, run with --add flag:")
        print(f"  python3 scripts/discover_markets.py --days {args.days} --add")


if __name__ == "__main__":
    main()
