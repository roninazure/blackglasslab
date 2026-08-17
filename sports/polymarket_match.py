from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


GAMMA_BASE_URL = "https://gamma-api.polymarket.com"


@dataclass(frozen=True)
class PolymarketMoneyline:
    event_id: str
    market_id: str
    slug: str
    event_date: str
    team_a: str
    team_b: str
    token_a: str
    token_b: str
    indicative_price_a: float | None
    indicative_price_b: float | None


def _normalize_team(value: str) -> str:
    return " ".join(
        re.sub(r"[^a-z0-9 ]+", " ", value.lower()).split()
    )


def _parse_json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value

    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []

        return parsed if isinstance(parsed, list) else []

    return []


def _slug_date(slug: str) -> str | None:
    match = re.search(r"(\d{4}-\d{2}-\d{2})$", slug)
    return match.group(1) if match else None


def fetch_polymarket_mlb_moneylines(
    *,
    limit: int = 1000,
    timeout: float = 20.0,
) -> list[PolymarketMoneyline]:
    params = urllib.parse.urlencode({
        "limit": min(max(limit, 1), 1000),
        "offset": 0,
        "active": "true",
        "closed": "false",
        "order": "liquidity",
        "ascending": "false",
    })

    url = f"{GAMMA_BASE_URL}/events?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "parallax-sports/0.1",
            "Accept": "application/json",
        },
    )

    with urllib.request.build_opener(
        urllib.request.ProxyHandler({})
    ).open(req, timeout=timeout) as response:
        events = json.load(response)

    rows: list[PolymarketMoneyline] = []

    for event in events:
        sport = event.get("sport")

        if not isinstance(sport, dict):
            continue

        if str(sport.get("sport") or "").lower() != "mlb":
            continue

        title = str(event.get("title") or "").strip()
        slug = str(event.get("slug") or "").strip()
        event_date = _slug_date(slug)

        if not title or not slug or event_date is None:
            continue

        candidates = [
            market
            for market in (event.get("markets") or [])
            if str(market.get("question") or "").strip() == title
        ]

        if len(candidates) != 1:
            continue

        market = candidates[0]
        outcomes = _parse_json_list(market.get("outcomes"))
        tokens = _parse_json_list(market.get("clobTokenIds"))
        prices = _parse_json_list(market.get("outcomePrices"))

        if len(outcomes) != 2 or len(tokens) != 2:
            continue

        def price(index: int) -> float | None:
            try:
                return float(prices[index])
            except (IndexError, TypeError, ValueError):
                return None

        rows.append(
            PolymarketMoneyline(
                event_id=str(event.get("id") or ""),
                market_id=str(market.get("id") or ""),
                slug=slug,
                event_date=event_date,
                team_a=str(outcomes[0]),
                team_b=str(outcomes[1]),
                token_a=str(tokens[0]),
                token_b=str(tokens[1]),
                indicative_price_a=price(0),
                indicative_price_b=price(1),
            )
        )

    return rows


def match_moneyline(
    *,
    home_team: str,
    away_team: str,
    start_time: str,
    polymarket_markets: list[PolymarketMoneyline],
    now: datetime | None = None,
) -> PolymarketMoneyline | None:
    now = now or datetime.now(timezone.utc)

    start = datetime.fromisoformat(
        start_time.replace("Z", "+00:00")
    ).astimezone(timezone.utc)

    # V1 is strictly pregame.
    if start <= now:
        return None

    event_date = start.date().isoformat()

    wanted = {
        _normalize_team(home_team),
        _normalize_team(away_team),
    }

    matches = []

    for market in polymarket_markets:
        if market.event_date != event_date:
            continue

        teams = {
            _normalize_team(market.team_a),
            _normalize_team(market.team_b),
        }

        if teams == wanted:
            matches.append(market)

    # Never guess through ambiguity.
    if len(matches) != 1:
        return None

    return matches[0]
