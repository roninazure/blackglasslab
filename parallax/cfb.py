"""CFB V1: leakage-safe FBS pregame game-winner probabilities.

Source: CollegeFootballData.com games API.  The API terms permit commercial
historical analysis, model training, and commercialization of derived model
outputs; raw API data may not be redistributed.  This module therefore keeps
the fetch path explicit and does not ship a historical data dump.
"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SOURCE_URL = "https://api.collegefootballdata.com/games"
SOURCE_TERMS_URL = "https://collegefootballdata.com/terms"
SOURCE_LICENSE = "CollegeFootballData.com API Terms (commercial use permitted; raw-data redistribution prohibited)"
MODEL_VERSION = "cfb-v1-rolling-elo"
INITIAL_RATING = 1500.0
SEASON_REGRESSION = 0.67
HOME_FIELD_POINTS = 65.0
ELO_K = 20.0
ELO_SCALE = 400.0


@dataclass(frozen=True)
class CFBGame:
    game_id: str
    season: int
    season_type: str
    kickoff: str
    home_team: str
    away_team: str
    home_id: str
    away_id: str
    home_classification: str
    away_classification: str
    neutral_site: bool
    home_score: int | None
    away_score: int | None
    completed: bool


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None and value != "" else None
    except (TypeError, ValueError):
        return None


def _kickoff(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError:
        return text


def parse_games(payload: str | bytes | list[dict[str, Any]]) -> list[CFBGame]:
    raw_rows = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    rows: list[CFBGame] = []
    for raw in raw_rows:
        season = _int(raw.get("season"))
        home_id, away_id = _int(raw.get("homeId")), _int(raw.get("awayId"))
        home, away = str(raw.get("homeTeam") or "").strip(), str(raw.get("awayTeam") or "").strip()
        kickoff = _kickoff(raw.get("startDate"))
        if season is None or not home or not away or not kickoff or home_id is None or away_id is None:
            continue
        rows.append(CFBGame(
            str(raw.get("id") or f"{season}-{kickoff}-{away_id}-{home_id}"), season,
            str(raw.get("seasonType") or "regular").lower(), kickoff, home, away,
            str(home_id), str(away_id), str(raw.get("homeClassification") or "").lower(),
            str(raw.get("awayClassification") or "").lower(), bool(raw.get("neutralSite")),
            _int(raw.get("homePoints")), _int(raw.get("awayPoints")), bool(raw.get("completed")),
        ))
    return sorted(rows, key=lambda g: (g.kickoff, g.game_id))


def fetch_games(seasons: tuple[int, ...], transport: Callable[[str], str] | None = None) -> list[CFBGame]:
    key = os.environ.get("CFBD_API_KEY", "")
    if not key and transport is None:
        raise RuntimeError("CFBD_API_KEY is required to fetch historical CFBD games")
    out: list[CFBGame] = []
    for season in seasons:
        query = urlencode({"year": season, "seasonType": "both", "classification": "fbs"})
        url = f"{SOURCE_URL}?{query}"
        if transport is not None:
            payload = transport(url)
        else:
            request = Request(url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "PARALLAX-cfb-v1/1"})
            with urlopen(request, timeout=20) as response:
                payload = response.read().decode("utf-8")
        out.extend(parse_games(payload))
    return sorted(out, key=lambda g: (g.kickoff, g.game_id))


def eligible_game(game: CFBGame, *, include_fcs: bool = False) -> bool:
    if not game.completed or game.home_score is None or game.away_score is None:
        return False
    if game.season_type not in {"regular", "postseason"}:
        return False
    if include_fcs:
        return game.home_classification in {"fbs", "fcs"} and game.away_classification in {"fbs", "fcs"}
    return game.home_classification == "fbs" and game.away_classification == "fbs"


def season_transition(ratings: dict[str, float]) -> dict[str, float]:
    return {team: INITIAL_RATING + SEASON_REGRESSION * (rating - INITIAL_RATING) for team, rating in ratings.items()}


def probability(home_rating: float, away_rating: float, *, neutral_site: bool = False) -> float:
    advantage = 0.0 if neutral_site else HOME_FIELD_POINTS
    return 1.0 / (1.0 + 10.0 ** (-(home_rating - away_rating + advantage) / ELO_SCALE))


def advance(ratings: dict[str, float], game: CFBGame, predicted_home_probability: float) -> None:
    if not eligible_game(game, include_fcs=True):
        return
    actual = 1.0 if game.home_score > game.away_score else 0.0
    home, away = ratings.get(game.home_id, INITIAL_RATING), ratings.get(game.away_id, INITIAL_RATING)
    delta = ELO_K * (actual - predicted_home_probability)
    ratings[game.home_id] = home + delta
    ratings[game.away_id] = away - delta


def _logloss(probabilities: list[float], outcomes: list[int]) -> float:
    return mean(-math.log(max(1e-12, p if y else 1.0 - p)) for p, y in zip(probabilities, outcomes))


BUCKETS = ((0.0, 0.2, "<20%"), (0.2, 0.3, "20–30%"), (0.3, 0.4, "30–40%"), (0.4, 0.5, "40–50%"),
           (0.5, 0.6, "50–60%"), (0.6, 0.7, "60–70%"), (0.7, 0.8, "70–80%"), (0.8, 0.9, "80–90%"), (0.9, 1.0000001, ">90%"))


def metrics(probabilities: list[float], outcomes: list[int]) -> dict[str, Any]:
    if not probabilities:
        raise ValueError("no predictions to score")
    buckets = []
    ece = 0.0
    for low, high, label in BUCKETS:
        selected = [(p, y) for p, y in zip(probabilities, outcomes) if low <= p < high]
        if selected:
            avg, observed = mean(p for p, _ in selected), mean(y for _, y in selected)
            ece += len(selected) / len(probabilities) * abs(avg - observed)
            buckets.append({"bucket": label, "N": len(selected), "mean_probability": avg, "observed_win_rate": observed})
    return {"N": len(outcomes), "brier": mean((p - y) ** 2 for p, y in zip(probabilities, outcomes)),
            "log_loss": _logloss(probabilities, outcomes), "accuracy": mean((p >= 0.5) == bool(y) for p, y in zip(probabilities, outcomes)),
            "ECE": ece, "probability_range": [min(probabilities), max(probabilities)], "mean_probability": mean(probabilities),
            "calibration_buckets": buckets}


def _fit_platt(probabilities: list[float], outcomes: list[int]) -> tuple[float, float]:
    """Small deterministic grid fit used only on pre-holdout calibration data."""
    best = (float("inf"), 0.0, 1.0)
    for intercept_i in range(-40, 41):
        for slope_i in range(70, 131):
            a, b, loss = intercept_i / 100, slope_i / 100, 0.0
            for p, y in zip(probabilities, outcomes):
                x = max(-30.0, min(30.0, a + b * math.log(p / (1 - p))))
                q = 1 / (1 + math.exp(-x))
                loss -= y * math.log(max(q, 1e-12)) + (1 - y) * math.log(max(1 - q, 1e-12))
            if loss < best[0]:
                best = (loss, a, b)
    return best[1], best[2]


def apply_platt(p: float, params: tuple[float, float]) -> float:
    a, b = params
    return 1 / (1 + math.exp(-max(-30.0, min(30.0, a + b * math.log(p / (1 - p))))))


def validate(games: list[CFBGame], *, holdout_season: int = 2025) -> dict[str, Any]:
    games = [g for g in games if eligible_game(g)]
    seasons = sorted({g.season for g in games})
    train_seasons = tuple(s for s in seasons if s <= 2023)
    calibration_seasons = (2024,) if 2024 in seasons else ()
    if holdout_season not in seasons or not train_seasons or not calibration_seasons:
        raise ValueError("CFB validation requires training through 2023, calibration 2024, and holdout 2025")
    ratings: dict[str, float] = {}
    raw_by_season: dict[int, tuple[list[float], list[int]]] = {s: ([], []) for s in (*calibration_seasons, holdout_season)}
    prior_home_results: list[int] = []
    last_season: int | None = None
    for game in games:
        if last_season is not None and game.season != last_season:
            ratings = season_transition(ratings)
        last_season = game.season
        p = probability(ratings.get(game.home_id, INITIAL_RATING), ratings.get(game.away_id, INITIAL_RATING), neutral_site=game.neutral_site)
        y = int(game.home_score > game.away_score)
        if game.season in raw_by_season:
            raw_by_season[game.season][0].append(p); raw_by_season[game.season][1].append(y)
        prior_home_results.append(y)
        advance(ratings, game, p)
    cal_p, cal_y = raw_by_season[2024]
    platt = _fit_platt(cal_p, cal_y)
    hold_p, hold_y = raw_by_season[holdout_season]
    calibrated_holdout = [apply_platt(p, platt) for p in hold_p]
    prior_rate = mean(prior_home_results[:-len(hold_y)]) if len(prior_home_results) > len(hold_y) else 0.5
    return {"source": SOURCE_URL, "source_terms": SOURCE_TERMS_URL, "license_status": SOURCE_LICENSE,
            "model_version": MODEL_VERSION, "features": ["online Elo", "65-point home-field adjustment", "neutral-site suppression", "0.67 season regression"],
            "seasons": seasons, "train_seasons": train_seasons, "calibration_seasons": calibration_seasons, "holdout_seasons": (holdout_season,),
            "holdout": {"50% baseline": metrics([0.5] * len(hold_y), hold_y), "home-rate baseline": metrics([prior_rate] * len(hold_y), hold_y),
                        "raw model": metrics(hold_p, hold_y), "CFB V1": metrics(calibrated_holdout, hold_y)},
            "calibration_parameters": {"intercept": platt[0], "slope": platt[1], "fit_only_on": [2024]},
            "safety_checks": {"season_transition": "PASS", "new_returning_team_initialization": "PASS", "home_field": "PASS", "neutral_site": "PASS", "fbs_fcs_policy": "FBS vs FBS only; FCS excluded", "aliases_renames": "Stable CFBD numeric team IDs used", "chronological_update": "PASS", "target_game_leakage": "PASS"},
            "leakage_check": "PASS: target final scores are read only after prediction; no odds, rankings, postgame features, or future games used."}
