#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sports.clob import fetch_top_of_book
from sports.evaluator import evaluate_two_way_moneyline
from sports.fair_value import consensus_two_way_moneyline
from sports.odds_provider import fetch_odds
from sports.polymarket_match import (
    fetch_polymarket_mlb_moneylines,
    match_moneyline,
)


DEFAULT_STAKE_USD = 10.0
DEFAULT_MIN_EDGE = 0.02
DEFAULT_MIN_BOOKS = 4
DEFAULT_MAX_QUOTE_AGE = 300.0

SPORT_KEYS = {
    "mlb": "baseball_mlb",
}


def fetch_market_metadata(condition_id: str) -> dict:
    url = f"https://clob.polymarket.com/clob-markets/{condition_id}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "parallax-sports/0.1",
            "Accept": "application/json",
        },
    )

    with urllib.request.urlopen(req, timeout=20) as response:
        return json.load(response)


def fee_rate_from_metadata(metadata: dict) -> float | None:
    details = metadata.get("fd")

    if not isinstance(details, dict):
        return None

    rate = details.get("r")

    if rate is None:
        return None

    try:
        return float(rate)
    except (TypeError, ValueError):
        return None


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parallax PAPER_ONLY sports opportunity scanner"
    )

    parser.add_argument(
        "--sport",
        choices=sorted(SPORT_KEYS),
        default="mlb",
    )
    parser.add_argument(
        "--stake",
        type=float,
        default=DEFAULT_STAKE_USD,
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=DEFAULT_MIN_EDGE,
    )
    parser.add_argument(
        "--output",
        default="reports/sports_scan_latest.json",
    )

    args = parser.parse_args()

    api_key = os.environ.get("THE_ODDS_API_KEY")

    if not api_key:
        raise SystemExit(
            "THE_ODDS_API_KEY is not set"
        )

    now = datetime.now(timezone.utc)

    sportsbook_events = fetch_odds(
        api_key=api_key,
        sport_key=SPORT_KEYS[args.sport],
        now=now,
    )

    if args.sport != "mlb":
        raise SystemExit(
            "Only MLB is enabled in Sports V1"
        )

    polymarket_markets = fetch_polymarket_mlb_moneylines()

    rows = []

    for event in sportsbook_events:
        fair = consensus_two_way_moneyline(
            list(event.moneylines),
            max_age_seconds=DEFAULT_MAX_QUOTE_AGE,
            min_books=DEFAULT_MIN_BOOKS,
        )

        if fair is None:
            continue

        poly = match_moneyline(
            home_team=event.home_team,
            away_team=event.away_team,
            start_time=event.start_time,
            polymarket_markets=polymarket_markets,
            now=now,
        )

        if poly is None:
            continue

        metadata = fetch_market_metadata(
            poly.condition_id
        )

        game_start = metadata.get("gst")

        if not game_start:
            continue

        # Stronger doubleheader/event-identity protection.
        sportsbook_start = parse_utc(event.start_time)
        polymarket_start = parse_utc(str(game_start))

        if abs(
            (sportsbook_start - polymarket_start).total_seconds()
        ) > 300:
            continue

        fee_rate = fee_rate_from_metadata(metadata)

        # Never pretend friction is known when it is not.
        if fee_rate is None:
            continue

        tokens = {
            poly.team_a: poly.token_a,
            poly.team_b: poly.token_b,
        }

        if (
            event.home_team not in tokens
            or event.away_team not in tokens
        ):
            continue

        home_book = fetch_top_of_book(
            tokens[event.home_team]
        )
        away_book = fetch_top_of_book(
            tokens[event.away_team]
        )

        result = evaluate_two_way_moneyline(
            team_a=event.home_team,
            team_b=event.away_team,
            fair_probability_a=fair.outcome_a_probability,
            book_a=home_book,
            book_b=away_book,
            stake_usd=args.stake,
            fee_rate=fee_rate,
            slippage_bps=0.0,
        )

        if result is None:
            continue

        economics = result.economics

        selected_fair = (
            result.fair_probability_a
            if result.selected_team == result.team_a
            else result.fair_probability_b
        )

        decision = (
            "PAPER_CANDIDATE"
            if economics.executable_edge >= args.min_edge
            and economics.expected_value_usd > 0
            and economics.depth_usd >= args.stake
            else "REJECT"
        )

        rows.append(
            {
                "sport": args.sport,
                "matchup": (
                    f"{event.away_team} @ "
                    f"{event.home_team}"
                ),
                "start_time": event.start_time,
                "polymarket_market_id": poly.market_id,
                "condition_id": poly.condition_id,
                "bookmakers_used": fair.sample_size,
                "selected_team": result.selected_team,
                "side": economics.side,
                "fair_probability": selected_fair,
                "entry_price": economics.entry_price,
                "raw_edge": economics.raw_edge,
                "fee_rate": fee_rate,
                "fee_usd": economics.fee_usd,
                "executable_edge": economics.executable_edge,
                "expected_value_usd": economics.expected_value_usd,
                "depth_usd": economics.depth_usd,
                "stake_usd": args.stake,
                "decision": decision,
            }
        )

    rows.sort(
        key=lambda row: row["executable_edge"],
        reverse=True,
    )

    payload = {
        "generated_at_utc": now.isoformat(),
        "mode": "PAPER_ONLY",
        "sport": args.sport,
        "stake_usd": args.stake,
        "minimum_executable_edge": args.min_edge,
        "evaluation_count": len(rows),
        "paper_candidate_count": sum(
            r["decision"] == "PAPER_CANDIDATE"
            for r in rows
        ),
        "reject_count": sum(
            r["decision"] == "REJECT"
            for r in rows
        ),
        "evaluations": rows,
    }

    output = Path(args.output)
    output.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    output.write_text(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )

    print("=== PARALLAX SPORTS SCAN ===")
    print("mode: PAPER_ONLY")
    print("sport:", args.sport.upper())
    print("evaluations:", len(rows))
    print(
        "PAPER_CANDIDATE:",
        payload["paper_candidate_count"],
    )
    print(
        "REJECT:",
        payload["reject_count"],
    )
    print()

    for index, row in enumerate(rows, 1):
        print(
            f"{index:02d} "
            f"{row['decision']:15} "
            f"{row['executable_edge']:+7.2%}  "
            f"{row['selected_team']}"
        )
        print(
            f"   fair={row['fair_probability']:.2%} "
            f"entry={row['entry_price']:.2%} "
            f"EV=${row['expected_value_usd']:+.3f} "
            f"depth=${row['depth_usd']:,.0f}"
        )

    print()
    print("artifact:", output)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
