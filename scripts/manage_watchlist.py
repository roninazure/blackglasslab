#!/usr/bin/env python3
"""
manage_watchlist.py — Autonomous Polymarket watchlist manager.

Runs daily. Fetches all active Polymarket markets, scores them by
liquidity, resolution timeline, and topic relevance (LLM-edge markets
like politics/macro/crypto rank higher; sports lotteries rank lower).

Usage:
    python3 scripts/manage_watchlist.py           # dry run — show what would change
    python3 scripts/manage_watchlist.py --apply   # apply changes to watchlist
"""
from __future__ import annotations

import argparse
import json
import re
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
MIN_PRICE       = 0.05       # skip extreme long-shots (YES < 5%)
PAGES_TO_SCAN   = 20         # 100 markets per page = 2000 markets scanned

# ---------------------------------------------------------------------------
# Topic scoring — where LLM reasoning produces real edge
# ---------------------------------------------------------------------------

# (keyword_list, score_multiplier)  — first match wins (ordered by priority)
TOPIC_BOOSTS: list[tuple[list[str], float]] = [
    # Elections / governance — highest edge
    (["election", "elected", "ballot", "referendum", "primary", "vote for", "polling"], 4.0),
    (["president", "congress", "senate", "parliament", "prime minister", "governor",
      "chancellor", "premier"], 3.5),
    (["trump", "harris", "biden", "executive order", "impeach", "indict",
      "convicted", "pardon"], 3.0),
    # Macro / Fed
    (["federal reserve", "rate cut", "rate hike", "fomc", "interest rate",
      "fed funds", "basis point"], 3.5),
    (["recession", "inflation", "gdp", "unemployment", "tariff", "cpi",
      "trade war", "default"], 3.0),
    # Geopolitics / conflict
    (["war", "ceasefire", "invasion", "nuclear", "missile", "conflict",
      "sanction", "coup", "assassination"], 3.0),
    (["supreme court", "scotus", "court ruling", "verdict", "ruling",
      "lawsuit", "indictment"], 3.0),
    # Crypto / tech
    (["bitcoin", " btc ", "ethereum", " eth ", "crypto", "solana", "ipo",
      "acquisition", "merger", "bankruptcy", "stock price"], 2.5),
    # Sports with reasoning value (playoff outcome — not individual team lottery)
    (["nfl season", "super bowl winner", "nba champion", "world series winner",
      "stanley cup winner", "masters winner", "world cup winner"], 1.5),
]

# Regex patterns → hard exclude (sports lotteries — individual team/player long-shots)
# Any market matching these is skipped entirely during scanning.
EXCLUDE_PATTERNS = [
    r"will .{3,40} win the \d{4} fifa world cup",
    r"will .{3,40} win the \d{4}[-–]\d{2,4} (champions league|la liga|premier league|bundesliga|serie a|ligue 1|eredivisie)",
    r"will .{3,40} win the \d{4} nba (finals|championship)",
    r"will .{3,40} win the \d{4} (world series|super bowl|stanley cup|grey cup)",
    r"will .{3,40} win the .{3,30} (division|conference)",
    r"(be|become) the (1st|first|2nd|second|third|3rd) (overall )?pick",
    r"win the \d{4}[-–]\d{2,4} (fa cup|league cup|copa del rey)",
    # Individual sports awards / trophies (scoring title, MVP races, etc.)
    r"will .{3,50} win the .{3,50} (trophy|award|title|golden boot|ballon d.or|mvp)",
    # League winner per-team markets (e.g. "Will Liverpool win the Premier League?")
    r"win the .{3,50} (premier league|la liga|bundesliga|serie a|ligue 1)",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_excluded(question: str) -> bool:
    """Return True if the market matches a sports-lottery exclusion pattern."""
    q = question.lower()
    return any(re.search(p, q) for p in EXCLUDE_PATTERNS)


def topic_multiplier(question: str) -> float:
    q = question.lower()
    for keywords, mult in TOPIC_BOOSTS:
        if any(kw.lower() in q for kw in keywords):
            return mult
    return 1.0   # neutral


def topic_label(question: str) -> str:
    q = question.lower()
    labels = [
        (["election", "ballot", "referendum", "primary"], "politics"),
        (["president", "congress", "senate", "parliament", "prime minister"], "politics"),
        (["trump", "harris", "biden", "executive order", "impeach"], "politics"),
        (["federal reserve", "rate cut", "fomc", "interest rate"], "macro/fed"),
        (["recession", "inflation", "gdp", "tariff", "cpi"], "macro/econ"),
        ([r"\bwar\b", "ceasefire", "invasion", "nuclear", "conflict", "sanction"], "geopolitics"),
        (["supreme court", "scotus", "verdict", "indictment"], "legal"),
        (["bitcoin", "btc", "ethereum", "eth", "crypto", "solana"], "crypto"),
        (["ipo", "acquisition", "merger", "bankruptcy"], "corporate"),
    ]
    for keywords, label in labels:
        for kw in keywords:
            if re.search(kw, q):
                return label
    return "other"


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


def best_price(m: dict) -> float | None:
    """Return the highest outcome price (the market's most-likely side)."""
    prices = m.get("outcomePrices")
    try:
        if isinstance(prices, str):
            prices = json.loads(prices)
        if isinstance(prices, list) and prices:
            return max(float(p) for p in prices)
    except Exception:
        pass
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


def score_market(volume: float, days: int, question: str) -> float:
    """Score = volume × recency × topic_multiplier."""
    recency = max(0.1, 1.0 - (days / 365))
    return volume * recency * topic_multiplier(question)


def load_watchlist() -> list[dict]:
    if not WATCHLIST_PATH.exists():
        return []
    return json.loads(WATCHLIST_PATH.read_text())


def save_watchlist(markets: list[dict]) -> None:
    WATCHLIST_PATH.write_text(json.dumps(markets, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    skipped_price = 0
    skipped_excluded = 0

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

            # Skip extreme long-shots (price < MIN_PRICE on best side)
            bp = best_price(m)
            if bp is not None and bp < MIN_PRICE:
                skipped_price += 1
                continue

            question = (m.get("question") or "")[:120]

            if is_excluded(question):
                skipped_excluded += 1
                continue

            entry = {
                "slug": slug,
                "question": question[:80],
                "days": days,
                "volume": volume,
                "score": score_market(volume, days, question),
                "topic": topic_label(question),
                "end_date": end_dt.strftime("%Y-%m-%d"),
            }

            if SHORT_TERM_DAYS[0] <= days <= SHORT_TERM_DAYS[1]:
                short_candidates.append(entry)
            elif LONG_TERM_DAYS[0] < days <= LONG_TERM_DAYS[1]:
                long_candidates.append(entry)

        time.sleep(0.1)

    print(f"Filtered {skipped_price} extreme long-shots (price < {MIN_PRICE:.0%})")
    print(f"Filtered {skipped_excluded} sports-lottery / individual-pick markets")

    # Sort by score descending
    short_candidates.sort(key=lambda x: x["score"], reverse=True)
    long_candidates.sort(key=lambda x: x["score"], reverse=True)

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
        print(f"  {tag}  {c['days']:3}d  ${c['volume']:>12,.0f}  [{c['topic']:<14}]  {c['slug'][:45]}")
        print(f"           {c['question']}")
    print()

    print(f"LONG-TERM   ({LONG_TERM_DAYS[0]}–{LONG_TERM_DAYS[1]}d)  [{len(long_picks)} selected]")
    for c in long_picks:
        tag = "NEW" if c["slug"] not in current_slugs else "   "
        print(f"  {tag}  {c['days']:3}d  ${c['volume']:>12,.0f}  [{c['topic']:<14}]  {c['slug'][:45]}")
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
