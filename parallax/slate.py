"""Shared dynamic sports-slate reconciliation.

The authoritative schedule defines the expected game set.  Market discovery may
produce zero, one, or many venue contracts for a game, but every scheduled game
is represented exactly once in the resulting slate report.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import Any


ACTIONS = ("BUY", "WATCH", "PASS")
ACTION_PRIORITY = {"PASS": 0, "WATCH": 1, "BUY": 2}
NON_PLAY_SCHEDULE_STATES = {"CANCELLED", "CANCELED", "POSTPONED", "SUSPENDED"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _game_record(game: Mapping[str, Any]) -> dict[str, Any]:
    game_id = _text(game.get("game_id"))
    if not game_id:
        raise ValueError("scheduled game requires game_id")
    date = _text(game.get("date"))
    if not date:
        raise ValueError(f"scheduled game {game_id} requires date")
    return {
        "game_id": game_id,
        "date": date,
        "start_time": _text(game.get("start_time")) or None,
        "away_team": _text(game.get("away_team")) or None,
        "home_team": _text(game.get("home_team")) or None,
        "schedule_status": (_text(game.get("schedule_status")) or "SCHEDULED").upper(),
    }


def reconcile_slate(
    scheduled_games: Iterable[Mapping[str, Any]],
    observations: Iterable[Mapping[str, Any]],
    *,
    discovery_complete: bool,
    data_unavailable_game_ids: Iterable[str] = (),
    mapping_failure_game_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Reconcile a dynamic authoritative schedule against evaluated market rows.

    Game-level action is the strongest evaluated side/venue action:
    BUY > WATCH > PASS.  Games without an evaluated action remain explicit:
    MAPPING_FAILURE, DATA_UNAVAILABLE, or NO_MARKET.
    """
    expected = [_game_record(game) for game in scheduled_games]
    ids = [game["game_id"] for game in expected]
    if len(ids) != len(set(ids)):
        raise ValueError("authoritative schedule contains duplicate game_id")

    expected_ids = set(ids)
    unavailable = {str(value) for value in data_unavailable_game_ids} & expected_ids
    mapping_failures = {str(value) for value in mapping_failure_game_ids} & expected_ids
    actions_by_game: dict[str, list[str]] = defaultdict(list)
    for row in observations:
        game_id = _text(row.get("game_id"))
        if game_id not in expected_ids:
            continue
        action = _text(row.get("action") or row.get("verdict")).upper()
        if action in ACTION_PRIORITY:
            actions_by_game[game_id].append(action)

    games_out: list[dict[str, Any]] = []
    for game in sorted(expected, key=lambda row: (row["date"], row["start_time"] or "", row["game_id"])):
        schedule_status = game["schedule_status"]
        actions = actions_by_game.get(game["game_id"], [])
        if schedule_status in NON_PLAY_SCHEDULE_STATES:
            status = "CANCELLED" if schedule_status == "CANCELED" else schedule_status
        elif actions:
            status = max(actions, key=ACTION_PRIORITY.__getitem__)
        elif game["game_id"] in mapping_failures:
            status = "MAPPING_FAILURE"
        elif game["game_id"] in unavailable or not discovery_complete:
            status = "DATA_UNAVAILABLE"
        else:
            status = "NO_MARKET"
        games_out.append({**game, "status": status})

    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for game in games_out:
        by_date[game["date"]].append(game)

    dates = []
    for date in sorted(by_date):
        rows = by_date[date]
        counts = Counter(row["status"] for row in rows)
        dates.append(
            {
                "date": date,
                "expected_games": len(rows),
                "accounted_games": len(rows),
                "all_games_accounted": len({row["game_id"] for row in rows}) == len(rows),
                "market_data_complete": not bool(
                    counts["DATA_UNAVAILABLE"] or counts["MAPPING_FAILURE"]
                ),
                "status_counts": dict(counts),
                "games": rows,
            }
        )

    counts = Counter(row["status"] for row in games_out)
    return {
        "expected_games": len(expected),
        "accounted_games": len(games_out),
        "all_games_accounted": len({row["game_id"] for row in games_out}) == len(expected),
        "market_data_complete": not bool(
            counts["DATA_UNAVAILABLE"] or counts["MAPPING_FAILURE"]
        ),
        "status_counts": dict(counts),
        "dates": dates,
    }
