#!/usr/bin/env python3
"""
manage_watchlist.py — Autonomous Polymarket watchlist manager.

Runs daily. Fetches all active Polymarket markets, scores them by
liquidity and resolution timeline, verifies slugs are priceable,
and writes a clean watchlist with the best short + long term markets.

Usage:
    python3 scripts/manage_watchlist.py           # dry run — show what would change
    python3 scripts/manage_watchlist.py --apply   # apply changes to watchlist
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

# Targets
SHORT_TERM_DAYS = (2, 30)    # resolves within 30 days
LONG_TERM_DAYS  = (30, 365)  # resolves within a year
SHORT_TERM_CAP  = 10         # max short-term slots
LONG_TERM_CAP   = 15         # max long-term slots
MIN_VOLUME      = 10_000     # minimum $ volume to qualify
PAGES_TO_SCAN   = 15         # 100 markets per page = 1500 markets scanned


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_page(offset: int) -> list:
    qs = urllib.parse.urlencode({
        "limit": 100,
        "offset": offset,
        "active": "true",
        "closed": "false",
    })
    req = urllib.request.Request(f"{GAMMA_BASE}/markets?{qs}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [fetch error offset={offset}]: {e}")
        return []


def parse_end_date(m: dict) -> datetime | None:
    for field in ["endDate", "endDateIso", "end_date"]:
        val = m.get(field)
        if not val:
            continue
        try:
            return datetime.fromisoformat(str(val).replace("Z", "+00:00"))
        except Exception:
            continue
    return None


def verify_slug(slug: str) -> bool:
    """Confirm slug returns real binary pricing from Gamma API."""
    qs = urllib.parse.urlencode({"slug": slug, "limit": 1})
    req = urllib.request.Request(f"{GAMMA_BASE}/markets?{qs}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not isinstance(data, list) or not data:
            return False
        m = data[0]
        outcomes = m.get("outcomes")
        prices = m.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        return (
            isinstance(outcomes, list) and
            isinstance(prices, list) and
            len(outcomes) >= 2 and
            len(prices) >= 2
        )
    except Exception:
        return False


def score_market(volume: float, days: int) -> float:
    """Score a market. Higher volume + sooner resolution = higher score."""
    recency = max(0.1, 1.0 - (days / 365))
    return volume * recency


def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    return json.loads(WATCHLIST_PATH.read_text())


def save_watchlist(markets: list[dict]) -> None:
    WATCHLIST_PATH.write_text(json.dumps(markets, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Autonomous Polymarket watchlist manager")
    ap.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    args = ap.parse_args()

    now = now_utc()
    current = load_watchlist()
    current_slugs = {m["market_id"] for m in current}

    print(f"WATCHLIST MANAGER — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Current watchlist: {len(current)} markets")
    print(f"Scanning {PAGES_TO_SCAN * 100} markets from Polymarket...")
    print()

    short_candidates = []
    long_candidates  = []
    seen_slugs = set()

    for page in range(PAGES_TO_SCAN):
        markets = fetch_page(page * 100)
        if not markets:
            break

        for m in markets:
            slug = m.get("slug", "").strip()
            if not slug or slug in seen_slugs:
                continue
            seen_slugs.add(slug)

            if not m.get("active") or m.get("closed"):
                continue

            end_dt = parse_end_date(m)
            if not end_dt:
                continue

            days = (end_dt - now).days
            volume = float(m.get("volume") or 0)

            if volume < MIN_VOLUME:
                continue

            entry = {
                "slug": slug,
                "question": (m.get("question") or "")[:80],
                "days": days,
                "volume": volume,
                "score": score_market(volume, days),
                "end_date": end_dt.strftime("%Y-%m-%d"),
            }

            if SHORT_TERM_DAYS[0] <= days <= SHORT_TERM_DAYS[1]:
                short_candidates.append(entry)
            elif LONG_TERM_DAYS[0] < days <= LONG_TERM_DAYS[1]:
                long_candidates.append(entry)

        time.sleep(0.1)

    # Sort by score descending
    short_candidates.sort(key=lambda x: x["score"], reverse=True)
    long_candidates.sort(key=lambda x: x["score"], reverse=True)

    # Take top N, verify slugs
    print(f"Found {len(short_candidates)} short-term candidates, {len(long_candidates)} long-term candidates")
    print("Verifying slugs...")
    print()

    def pick_verified(candidates: list, cap: int) -> list:
        verified = []
        for c in candidates:
            if len(verified) >= cap:
                break
            if verify_slug(c["slug"]):
                verified.append(c)
            time.sleep(0.15)
        return verified

    short_picks = pick_verified(short_candidates, SHORT_TERM_CAP)
    long_picks  = pick_verified(long_candidates,  LONG_TERM_CAP)
    all_picks   = short_picks + long_picks
    new_slugs   = {c["slug"] for c in all_picks}

    # Determine changes
    to_add    = [c for c in all_picks if c["slug"] not in current_slugs]
    to_remove = [m for m in current if m["market_id"] not in new_slugs]
    to_keep   = [m for m in current if m["market_id"] in new_slugs]

    # Report
    print(f"SHORT-TERM  ({SHORT_TERM_DAYS[0]}–{SHORT_TERM_DAYS[1]}d)  [{len(short_picks)} selected]")
    for c in short_picks:
        tag = "NEW" if c["slug"] not in current_slugs else "   "
        print(f"  {tag}  {c['days']:3}d  ${c['volume']:>12,.0f}  {c['slug'][:52]}")
        print(f"           {c['question']}")
    print()

    print(f"LONG-TERM   ({LONG_TERM_DAYS[0]}–{LONG_TERM_DAYS[1]}d)  [{len(long_picks)} selected]")
    for c in long_picks:
        tag = "NEW" if c["slug"] not in current_slugs else "   "
        print(f"  {tag}  {c['days']:3}d  ${c['volume']:>12,.0f}  {c['slug'][:52]}")
        print(f"           {c['question']}")
    print()

    print(f"CHANGES:  +{len(to_add)} add  -{len(to_remove)} remove  ={len(to_keep)} keep")
    if to_remove:
        print("REMOVING:")
        for m in to_remove:
            print(f"  - {m['market_id']}")
    print()

    if not args.apply:
        print("DRY RUN — no changes made. Run with --apply to update watchlist.")
        return

    # Build new watchlist
    new_watchlist = (
        [{"market_id": c["slug"]} for c in short_picks] +
        [{"market_id": c["slug"]} for c in long_picks]
    )
    save_watchlist(new_watchlist)
    print(f"Watchlist updated: {len(new_watchlist)} markets ({len(short_picks)} short + {len(long_picks)} long)")


if __name__ == "__main__":
    main()
