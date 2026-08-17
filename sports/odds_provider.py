from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone

from .devig import american_to_decimal
from .fair_value import BookMoneyline


DEFAULT_BASE_URL = "https://api.theoddsapi.com"


@dataclass(frozen=True)
class SportsEvent:
    event_id: str
    sport: str
    league: str
    home_team: str
    away_team: str
    start_time: str
    moneylines: tuple[BookMoneyline, ...]


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def _quote_age_seconds(updated_at: str, now: datetime) -> float:
    updated = _parse_utc(updated_at)
    return max(0.0, (now - updated).total_seconds())


def fetch_odds(
    *,
    api_key: str,
    sport_key: str,
    timeout: float = 20.0,
    now: datetime | None = None,
) -> list[SportsEvent]:
    if not api_key:
        raise ValueError("api_key is required")

    now = now or datetime.now(timezone.utc)

    url = f"{DEFAULT_BASE_URL}/odds/?sport_key={sport_key}"

    req = urllib.request.Request(
        url,
        headers={
            "x-api-key": api_key,
            "Accept": "application/json",
            "User-Agent": "parallax-sports/0.1",
        },
    )

    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = json.load(r)

    if payload.get("success") is not True:
        raise RuntimeError(
            f"odds provider returned unsuccessful response: {payload!r}"
        )

    events: list[SportsEvent] = []

    for event in payload.get("data") or []:
        home = str(event.get("home_team") or "").strip()
        away = str(event.get("away_team") or "").strip()

        if not home or not away:
            continue

        moneylines: list[BookMoneyline] = []

        for book in event.get("books") or []:
            if book.get("market") != "h2h":
                continue

            bookmaker = str(book.get("book") or "").strip()
            updated_at = str(book.get("updated_at") or "").strip()
            outcomes = book.get("outcomes") or []

            if not bookmaker or not updated_at:
                continue

            prices = {}

            for outcome in outcomes:
                name = str(outcome.get("name") or "").strip()
                price = outcome.get("price")

                if not name or price is None:
                    continue

                prices[name] = price

            if home not in prices or away not in prices:
                continue

            try:
                home_decimal = american_to_decimal(prices[home])
                away_decimal = american_to_decimal(prices[away])
                age_seconds = _quote_age_seconds(updated_at, now)
            except (TypeError, ValueError):
                continue

            moneylines.append(
                BookMoneyline(
                    bookmaker=bookmaker,
                    outcome_a_decimal=home_decimal,
                    outcome_b_decimal=away_decimal,
                    age_seconds=age_seconds,
                )
            )

        events.append(
            SportsEvent(
                event_id=str(event.get("event_id") or ""),
                sport=str(event.get("sport") or ""),
                league=str(event.get("league") or ""),
                home_team=home,
                away_team=away,
                start_time=str(event.get("start_time") or ""),
                moneylines=tuple(moneylines),
            )
        )

    return events
