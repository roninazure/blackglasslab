#!/usr/bin/env python3
"""
manage_watchlist.py — Autonomous Polymarket watchlist manager.

Runs daily. Validates existing watchlist markets, keeps good ones,
and fills empty slots with fresh high-edge candidates from a scan
of 2000 Polymarket markets.

Strategy:
  • Existing markets stay until they expire or close (sticky)
  • New picks only fill open slots (up to cap)
  • Sports lotteries / individual-pick markets are hard-excluded
  • LLM-edge topics (politics, macro, crypto, geopolitics) score higher

Usage:
    python3 scripts/manage_watchlist.py           # dry run
    python3 scripts/manage_watchlist.py --apply   # apply changes
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

SHORT_TERM_DAYS = (2, 30)
LONG_TERM_DAYS  = (30, 365)
SHORT_TERM_CAP  = 10
LONG_TERM_CAP   = 15
MIN_VOLUME      = 10_000
MIN_PRICE       = 0.05
PAGES_TO_SCAN   = 20
# Max markets per topic across the entire watchlist (prevents Fed-cut over-concentration)
MAX_PER_TOPIC   = 2

# ---------------------------------------------------------------------------
# Topic scoring
# ---------------------------------------------------------------------------

TOPIC_BOOSTS: list[tuple[list[str], float]] = [
    (["election", "elected", "ballot", "referendum", "primary", "vote for"], 4.0),
    (["president", "congress", "senate", "parliament", "prime minister",
      "governor", "chancellor", "premier"], 3.5),
    (["trump", "harris", "biden", "executive order", "impeach", "indict",
      "convicted", "pardon"], 3.0),
    (["federal reserve", "rate cut", "rate hike", "fomc", "interest rate",
      "fed funds", "basis point"], 3.5),
    (["recession", "inflation", "gdp", "unemployment", "tariff", "cpi",
      "trade war", "default"], 3.0),
    (["ceasefire", "invasion", "nuclear", "missile", "conflict",
      "sanction", "coup", "assassination"], 3.0),
    (["supreme court", "scotus", "court ruling", "verdict",
      "lawsuit", "indictment"], 3.0),
    (["bitcoin", " btc ", "ethereum", " eth ", "crypto", "solana"], 2.5),
    (["ipo", "acquisition", "merger", "bankruptcy"], 2.0),
]

# Hard-exclude patterns — these markets are never picked
EXCLUDE_PATTERNS = [
    # Team wins a specific tournament
    r"will .{3,40} win the \d{4} fifa world cup",
    r"will .{3,40} win the \d{4}.{0,12}(stanley cup|grey cup|world series|super bowl)",
    r"will .{3,40} win the \d{4} nba (finals|championship)",
    r"will .{3,40} win the \d{4}[-–]\d{2,4}.{0,20}(champions league|europa league|conference league|la liga|premier league|bundesliga|serie a|ligue 1|eredivisie)",
    r"win the .{3,60}(champions league|europa league|conference league|premier league|la liga|bundesliga|serie a|ligue 1)",
    # Division / conference winner
    r"will .{3,40} win the .{3,30} (division|conference)",
    # Playoff / postseason qualification (any sport)
    r"will .{3,50} (make|reach|qualify for) the .{0,20}(nhl|nba|nfl|mlb|mls|wnba).{0,15}(playoff|post.?season)",
    r"will .{3,50} (make|reach|qualify for) the .{0,10}playoff",
    r"(nhl|nba|nfl|mlb|mls|wnba) .{0,30}(playoff|postseason) .{0,20}(berth|spot|seed|qualifier)",
    # Individual draft picks
    r"(be|become) the (1st|first|2nd|second|third|3rd) (overall )?pick",
    # Individual sports awards and trophies
    r"will .{3,60} win the .{3,60}(trophy|award|golden boot|ballon d.or|\bmvp\b)",
    # Cup/domestic competitions per-team
    r"win the \d{4}[-–]\d{2,4}.{0,10}(fa cup|league cup|copa del rey|dfb-pokal)",
    # Relegation / promotion markets
    r"will .{3,50} be relegated",
    r"will .{3,50} (get|avoid) relegat",
    r"will .{3,50} (win|earn|secure) promot",
    # Meme / novelty markets
    r"(jesus christ|second coming|rapture|will god|will aliens|flat earth|lizard people)",
]


# ---------------------------------------------------------------------------
# Core helpers
# ---------------------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def is_excluded(question: str) -> bool:
    q = question.lower()
    return any(re.search(p, q) for p in EXCLUDE_PATTERNS)


def topic_multiplier(question: str) -> float:
    q = question.lower()
    for keywords, mult in TOPIC_BOOSTS:
        if any(kw.lower() in q for kw in keywords):
            return mult
    return 1.0


def topic_label(question: str) -> str:
    q = question.lower()
    checks = [
        (["election", "ballot", "referendum", "primary"],               "politics"),
        (["president", "congress", "senate", "parliament", "prime minister"], "politics"),
        (["trump", "harris", "biden", "executive order", "impeach"],    "politics"),
        (["federal reserve", "rate cut", "fomc", "interest rate"],      "macro/fed"),
        (["recession", "inflation", "gdp", "tariff", "cpi"],            "macro/econ"),
        ([r"\bceasefire\b", r"\binvasion\b", r"\bnuclear\b",
          r"\bconflict\b", r"\bsanction\b"],                            "geopolitics"),
        (["supreme court", "scotus", "verdict", "indictment"],          "legal"),
        (["bitcoin", "btc", "ethereum", "eth", "crypto", "solana"],     "crypto"),
        (["ipo", "acquisition", "merger", "bankruptcy"],                "corporate"),
    ]
    for keywords, label in checks:
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
    qs = urllib.parse.urlencode({"slug": slug, "limit": 1})
    req = urllib.request.Request(f"{GAMMA_BASE}/markets?{qs}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not isinstance(data, list) or not data:
            return False
        m = data[0]
        outcomes = m.get("outcomes")
        prices   = m.get("outcomePrices")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(prices, str):
            prices = json.loads(prices)
        return (
            isinstance(outcomes, list) and isinstance(prices, list) and
            len(outcomes) >= 2 and len(prices) >= 2
        )
    except Exception:
        return False


def check_existing(slug: str, now: datetime) -> dict | None:
    """
    Validate an existing watchlist slug.
    Returns a status dict if still valid, None if expired/closed/gone.
    """
    qs = urllib.parse.urlencode({"slug": slug, "limit": 1})
    req = urllib.request.Request(f"{GAMMA_BASE}/markets?{qs}", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not isinstance(data, list) or not data:
            return None
        m = data[0]
        if not m.get("active") or m.get("closed"):
            return None
        end_dt = parse_end_date(m)
        if not end_dt:
            return None
        days = (end_dt - now).days
        if days < SHORT_TERM_DAYS[0]:
            return None   # resolving imminently — let it expire
        volume   = float(m.get("volume") or 0)
        question = (m.get("question") or "")[:80]
        if is_excluded(question):
            return None   # evict sports/noise markets even if still active
        return {
            "slug":     slug,
            "question": question,
            "days":     days,
            "volume":   volume,
            "topic":    topic_label(question),
            "end_date": end_dt.strftime("%Y-%m-%d"),
        }
    except Exception:
        return None


def score_market(volume: float, days: int, question: str) -> float:
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    now     = now_utc()
    current = load_watchlist()

    print(f"WATCHLIST MANAGER — {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Current watchlist: {len(current)} markets")
    print()

    # ------------------------------------------------------------------
    # Step 1: validate existing markets — keep what's still good
    # ------------------------------------------------------------------
    print("Validating existing watchlist...")
    valid_short: list[dict] = []
    valid_long:  list[dict] = []
    expired:     list[str]  = []

    for entry in current:
        slug = entry["market_id"]
        info = check_existing(slug, now)
        if info is None:
            expired.append(slug)
        elif SHORT_TERM_DAYS[0] <= info["days"] <= SHORT_TERM_DAYS[1]:
            valid_short.append(info)
        else:
            valid_long.append(info)
        time.sleep(0.15)

    valid_slugs  = {m["slug"] for m in valid_short + valid_long}
    short_slots  = SHORT_TERM_CAP - len(valid_short)
    long_slots   = LONG_TERM_CAP  - len(valid_long)

    print(f"  kept   {len(valid_short)} short-term  {len(valid_long)} long-term")
    if expired:
        print(f"  expired/closed: {len(expired)}")
        for s in expired:
            print(f"    - {s}")
    print(f"  open slots: {short_slots} short  {long_slots} long")
    print()

    # ------------------------------------------------------------------
    # Step 2: scan for new candidates to fill open slots
    # ------------------------------------------------------------------
    if short_slots <= 0 and long_slots <= 0:
        print("All slots filled by existing markets. Nothing to scan.")
    else:
        print(f"Scanning {PAGES_TO_SCAN * 100} markets from Polymarket...")

    short_candidates: list[dict] = []
    long_candidates:  list[dict] = []
    seen_slugs = set(valid_slugs)
    skipped_excluded = 0

    if short_slots > 0 or long_slots > 0:
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

                days   = (end_dt - now).days
                volume = float(m.get("volume") or 0)

                if volume < MIN_VOLUME:
                    continue

                bp = best_price(m)
                if bp is not None and bp < MIN_PRICE:
                    continue

                question = (m.get("question") or "")[:120]

                if is_excluded(question):
                    skipped_excluded += 1
                    continue

                entry = {
                    "slug":     slug,
                    "question": question[:80],
                    "days":     days,
                    "volume":   volume,
                    "score":    score_market(volume, days, question),
                    "topic":    topic_label(question),
                    "end_date": end_dt.strftime("%Y-%m-%d"),
                }

                if SHORT_TERM_DAYS[0] <= days <= SHORT_TERM_DAYS[1]:
                    short_candidates.append(entry)
                elif LONG_TERM_DAYS[0] < days <= LONG_TERM_DAYS[1]:
                    long_candidates.append(entry)

            time.sleep(0.1)

        short_candidates.sort(key=lambda x: x["score"], reverse=True)
        long_candidates.sort(key=lambda x: x["score"], reverse=True)
        print(f"Filtered {skipped_excluded} sports-lottery / individual-pick markets")
        print(f"Found {len(short_candidates)} short-term candidates, {len(long_candidates)} long-term candidates")

    print("Verifying new candidates...")
    print()

    # Build topic counts from already-validated markets so concentration check is global
    existing_topic_counts: dict[str, int] = {}
    for m in valid_short + valid_long:
        t = m.get("topic", "other")
        existing_topic_counts[t] = existing_topic_counts.get(t, 0) + 1

    def pick_verified(candidates: list, cap: int, topic_counts: dict) -> list:
        verified = []
        for c in candidates:
            if len(verified) >= cap:
                break
            t = c.get("topic", "other")
            # "other" topics are unconstrained; named topics capped at MAX_PER_TOPIC
            if t != "other" and topic_counts.get(t, 0) >= MAX_PER_TOPIC:
                continue
            if verify_slug(c["slug"]):
                verified.append(c)
                topic_counts[t] = topic_counts.get(t, 0) + 1
            time.sleep(0.15)
        return verified

    short_new = pick_verified(short_candidates, max(0, short_slots), existing_topic_counts)
    long_new  = pick_verified(long_candidates,  max(0, long_slots),  existing_topic_counts)

    # ------------------------------------------------------------------
    # Step 3: report
    # ------------------------------------------------------------------
    final_short = valid_short + short_new
    final_long  = valid_long  + long_new
    new_pick_slugs = {c["slug"] for c in short_new + long_new}

    print(f"SHORT-TERM  ({SHORT_TERM_DAYS[0]}–{SHORT_TERM_DAYS[1]}d)  [{len(final_short)} / {SHORT_TERM_CAP} slots]")
    for c in final_short:
        tag = "NEW" if c["slug"] in new_pick_slugs else "   "
        print(f"  {tag}  {c['days']:3}d  ${c['volume']:>12,.0f}  [{c['topic']:<14}]  {c['slug'][:45]}")
        print(f"           {c['question']}")
    if not final_short:
        print("  (no qualifying short-term markets found)")
    print()

    print(f"LONG-TERM   ({LONG_TERM_DAYS[0]}–{LONG_TERM_DAYS[1]}d)  [{len(final_long)} / {LONG_TERM_CAP} slots]")
    for c in final_long:
        tag = "NEW" if c["slug"] in new_pick_slugs else "   "
        print(f"  {tag}  {c['days']:3}d  ${c['volume']:>12,.0f}  [{c['topic']:<14}]  {c['slug'][:45]}")
        print(f"           {c['question']}")
    print()

    to_add    = short_new + long_new
    to_remove = expired
    to_keep   = valid_short + valid_long

    # Topic concentration summary
    all_final = final_short + final_long
    topic_summary: dict[str, int] = {}
    for m in all_final:
        t = m.get("topic", "other")
        topic_summary[t] = topic_summary.get(t, 0) + 1
    print("TOPIC CONCENTRATION:")
    for t, n in sorted(topic_summary.items(), key=lambda x: -x[1]):
        warn = " !! OVER CAP" if t != "other" and n > MAX_PER_TOPIC else ""
        print(f"  {t:<16} {n}{warn}")
    print()

    print(f"CHANGES:  +{len(to_add)} add  -{len(to_remove)} remove  ={len(to_keep)} keep")
    if to_remove:
        print("REMOVING (expired/closed):")
        for s in to_remove:
            print(f"  - {s}")
    print()

    if not args.apply:
        print("DRY RUN — no changes made. Run with --apply to update watchlist.")
        return

    new_watchlist = (
        [{"market_id": c["slug"]} for c in final_short] +
        [{"market_id": c["slug"]} for c in final_long]
    )
    save_watchlist(new_watchlist)
    print(f"Watchlist updated: {len(new_watchlist)} markets "
          f"({len(final_short)} short + {len(final_long)} long)")


if __name__ == "__main__":
    main()
