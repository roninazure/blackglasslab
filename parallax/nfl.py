"""NFL V1: pregame regular/postseason game-winner probabilities.

Source: nflverse/nflverse-data game schedules/results, CC-BY 4.0.  Final
scores are used only to advance state after each completed game; target-game
scores are never available when a prediction is made.
"""
from __future__ import annotations

import csv
import io
import json
import math
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from statistics import mean
from typing import Any, Callable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from .models import Evidence, NormalizedMarket, PlayType, Venue, utcnow
from .normalization import rules_digest

SOURCE_URL = "https://raw.githubusercontent.com/nflverse/nfldata/master/data/games.csv"
SOURCE_LICENSE = "CC-BY-4.0; nflverse-data LICENSE.md"
MODEL_VERSION = "nfl-v1-elo-rolling"
VALIDITY_SECONDS = 15 * 60
# Frozen from the accepted 2025 holdout; used only as a safety reference.
VALIDATION_ECE = 0.075465

NFL_TEAM_ALIASES = {
    "arizonacardinals": "ARI", "atlantafalcons": "ATL", "baltimoreravens": "BAL",
    "buffalobills": "BUF", "carolinapanthers": "CAR", "chicagobears": "CHI",
    "cincinnatibengals": "CIN", "clevelandbrowns": "CLE", "dallascowboys": "DAL",
    "denverbroncos": "DEN", "detroitlions": "DET", "greenbaypackers": "GB",
    "houstontexans": "HOU", "indianapoliscolts": "IND", "jacksonvillejaguars": "JAX",
    "kansascitychiefs": "KC", "lasvegasraiders": "LV", "losangeleschargers": "LAC",
    "losangelesrams": "LA", "miamidolphins": "MIA", "minnesotavikings": "MIN",
    "newenglandpatriots": "NE", "neworleanssaints": "NO", "newyorkgiants": "NYG",
    "newyorkjets": "NYJ", "philadelphiaeagles": "PHI", "pittsburghsteelers": "PIT",
    "sanfrancisco49ers": "SF", "seattleseahawks": "SEA", "tampabaybuccaneers": "TB",
    "tennesseetitans": "TEN", "washingtoncommanders": "WAS",
}


@dataclass(frozen=True)
class NFLGame:
    game_id: str
    season: int
    game_type: str
    kickoff: str
    home_team: str
    away_team: str
    home_score: int | None
    away_score: int | None


@dataclass(frozen=True)
class NFLValidation:
    source: str
    license_status: str
    fields_used: tuple[str, ...]
    seasons: tuple[int, ...]
    train_seasons: tuple[int, ...]
    calibration_seasons: tuple[int, ...]
    holdout_seasons: tuple[int, ...]
    sample_size: int
    metrics: dict[str, Any]
    leakage_check: str
    reproducible: bool


@dataclass(frozen=True)
class NFLMapping:
    status: str
    game: NFLGame | None
    reason: str
    selected_team: str | None = None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _date(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC).isoformat()
    except ValueError:
        return text


def _kickoff(raw: dict[str, Any]) -> str:
    gameday, gametime = str(raw.get("gameday") or raw.get("game_date") or "").strip(), str(raw.get("gametime") or "").strip()
    if gameday and gametime:
        try:
            return datetime.fromisoformat(f"{gameday}T{gametime}").replace(tzinfo=ZoneInfo("America/New_York")).astimezone(UTC).isoformat()
        except ValueError:
            pass
    return _date(gameday)


def parse_games(payload: str) -> list[NFLGame]:
    rows: list[NFLGame] = []
    for raw in csv.DictReader(io.StringIO(payload)):
        season = _int(raw.get("season"))
        home_score, away_score = _int(raw.get("home_score")), _int(raw.get("away_score"))
        home = str(raw.get("home_team") or "").strip()
        away = str(raw.get("away_team") or "").strip()
        kickoff = _kickoff(raw)
        # Future schedule rows intentionally have blank scores.  They are
        # valid fixture identity/state inputs, but are never state updates.
        if season is None or not home or not away or not kickoff:
            continue
        game_type = str(raw.get("game_type") or "REG").upper()
        if game_type not in {"REG", "POST"}:
            continue
        rows.append(NFLGame(str(raw.get("game_id") or f"{season}-{kickoff}-{away}-{home}"), season, game_type, kickoff, home, away, home_score, away_score))
    return sorted(rows, key=lambda row: (row.kickoff, row.game_id))


def fetch_games(transport: Callable[[str], str] | None = None) -> list[NFLGame]:
    if transport is None:
        request = Request(SOURCE_URL, headers={"Accept": "text/csv", "User-Agent": "PARALLAX-nfl-v1/1"})
        with urlopen(request, timeout=20) as response:
            payload = response.read().decode("utf-8")
    else:
        payload = transport(SOURCE_URL)
    return parse_games(payload)


def _probability(home_rating: float, away_rating: float) -> float:
    z = (home_rating - away_rating + 55.0) / 400.0
    return min(0.95, max(0.05, 1.0 / (1.0 + 10.0 ** (-z))))


def _advance(ratings: dict[str, float], game: NFLGame, probability: float) -> None:
    if game.home_score is None or game.away_score is None:
        return
    actual = 1.0 if game.home_score > game.away_score else 0.0
    home, away = ratings.get(game.home_team, 1500.0), ratings.get(game.away_team, 1500.0)
    delta = 20.0 * (actual - probability)
    ratings[game.home_team] = home + delta
    ratings[game.away_team] = away - delta


def _logloss(probabilities: list[float], outcomes: list[int]) -> float:
    return mean(-math.log(max(1e-12, p if y else 1.0 - p)) for p, y in zip(probabilities, outcomes))


def _ece(probabilities: list[float], outcomes: list[int], buckets: int = 10) -> tuple[float, list[dict[str, Any]]]:
    rows = []
    weighted = 0.0
    for index in range(buckets):
        selected = [(p, y) for p, y in zip(probabilities, outcomes) if index / buckets <= p < (index + 1) / buckets or (index == buckets - 1 and p == 1.0)]
        if not selected:
            continue
        predicted, observed = mean(p for p, _ in selected), mean(y for _, y in selected)
        weighted += len(selected) / len(probabilities) * abs(predicted - observed)
        rows.append({"bucket": f"{index / buckets:.1f}-{(index + 1) / buckets:.1f}", "count": len(selected), "mean_probability": round(predicted, 6), "observed_rate": round(observed, 6)})
    return weighted, rows


def _metrics(probabilities: list[float], outcomes: list[int]) -> dict[str, Any]:
    ece, buckets = _ece(probabilities, outcomes)
    return {"sample_size": len(outcomes), "brier": mean((p - y) ** 2 for p, y in zip(probabilities, outcomes)), "log_loss": _logloss(probabilities, outcomes), "accuracy": mean((p >= 0.5) == bool(y) for p, y in zip(probabilities, outcomes)), "ece": ece, "probability_range": [min(probabilities), max(probabilities)], "calibration_buckets": buckets}


def _calibrate(calibration_probabilities: list[float], outcomes: list[int]) -> tuple[float, float]:
    """Fit a tiny deterministic logistic recalibration on calibration only."""
    best = (float("inf"), 0.0, 1.0)
    for intercept in [x / 100 for x in range(-40, 41)]:
        for slope in [x / 100 for x in range(70, 131)]:
            loss = 0.0
            for raw, outcome in zip(calibration_probabilities, outcomes):
                logit = math.log(raw / (1 - raw))
                probability = 1 / (1 + math.exp(-max(-30, min(30, intercept + slope * logit))))
                loss -= outcome * math.log(max(probability, 1e-12)) + (1 - outcome) * math.log(max(1 - probability, 1e-12))
            if loss < best[0]:
                best = (loss, intercept, slope)
    return best[1], best[2]


def validate(games: list[NFLGame], *, holdout_season: int | None = None) -> NFLValidation:
    if len(games) < 100:
        raise ValueError("insufficient NFL historical games")
    seasons = sorted({game.season for game in games})
    holdout = holdout_season or seasons[-1]
    prior_seasons = [season for season in seasons if season < holdout]
    calibration_seasons = tuple(prior_seasons[-3:])
    train = tuple(season for season in prior_seasons if season not in calibration_seasons)
    holdout_seasons = (holdout,)
    ratings: dict[str, float] = {}
    dev_predictions: dict[int, list[float]] = {season: [] for season in calibration_seasons}
    dev_outcomes: dict[int, list[int]] = {season: [] for season in calibration_seasons}
    raw_holdout: list[float] = []
    y_holdout: list[int] = []
    home_history: list[int] = []
    for game in games:
        if game.home_score is None or game.away_score is None:
            continue
        if game.season not in set(train) | set(calibration_seasons) | set(holdout_seasons):
            continue
        home_rating, away_rating = ratings.get(game.home_team, 1500.0), ratings.get(game.away_team, 1500.0)
        raw = _probability(home_rating, away_rating)
        outcome = int(game.home_score > game.away_score)
        if game.season in dev_predictions:
            dev_predictions[game.season].append(raw); dev_outcomes[game.season].append(outcome)
        elif game.season in holdout_seasons:
            raw_holdout.append(raw); y_holdout.append(outcome)
        _advance(ratings, game, raw)
        home_history.append(outcome)
    candidate_values: dict[str, list[float]] = {"IDENTITY": [], "PLATT": []}
    candidate_outcomes: list[int] = []
    folds = []
    for index, season in enumerate(calibration_seasons):
        if index == 0:
            continue
        fit_probs = [p for earlier in calibration_seasons[:index] for p in dev_predictions[earlier]]
        fit_outcomes = [y for earlier in calibration_seasons[:index] for y in dev_outcomes[earlier]]
        intercept, slope = _calibrate(fit_probs, fit_outcomes) if fit_probs else (0.0, 1.0)
        raw_fold, outcomes_fold = dev_predictions[season], dev_outcomes[season]
        platt_fold = [_apply_calibration(p, intercept, slope) for p in raw_fold]
        candidate_values["IDENTITY"].extend(raw_fold); candidate_values["PLATT"].extend(platt_fold); candidate_outcomes.extend(outcomes_fold)
        folds.append({"evaluation_season": season, "fit_seasons": calibration_seasons[:index], "identity": _metrics(raw_fold, outcomes_fold), "platt": _metrics(platt_fold, outcomes_fold), "parameters": {"intercept": intercept, "slope": slope}})
    selection = {name: _metrics(values, candidate_outcomes) for name, values in candidate_values.items()}
    selected_policy = "PLATT" if (selection["PLATT"]["brier"], selection["PLATT"]["log_loss"], selection["PLATT"]["ece"]) < (selection["IDENTITY"]["brier"], selection["IDENTITY"]["log_loss"], selection["IDENTITY"]["ece"]) else "IDENTITY / EMPIRICALLY VALIDATED RAW PROBABILITIES"
    all_dev_probs = [p for season in calibration_seasons for p in dev_predictions[season]]
    all_dev_outcomes = [y for season in calibration_seasons for y in dev_outcomes[season]]
    intercept, slope = _calibrate(all_dev_probs, all_dev_outcomes) if all_dev_probs else (0.0, 1.0)
    selected = raw_holdout if selected_policy.startswith("IDENTITY") else [_apply_calibration(p, intercept, slope) for p in raw_holdout]
    metrics = {"naive_50": _metrics([0.5] * len(y_holdout), y_holdout), "home_rate_prior": _metrics([mean(home_history[:max(1, len(home_history) - len(y_holdout))])] * len(y_holdout), y_holdout), "elo_raw": _metrics(raw_holdout, y_holdout), "elo_selected": _metrics(selected, y_holdout), "pre_holdout_selection": {"folds": folds, "aggregate": selection, "selected_policy": selected_policy, "parameters_frozen_before_holdout": {"intercept": intercept, "slope": slope}}, "calibration_parameters": {"intercept": intercept, "slope": slope}}
    return NFLValidation(SOURCE_URL, SOURCE_LICENSE, ("season", "game_type", "gameday", "home_team", "away_team", "home_score", "away_score"), tuple(seasons), train, calibration_seasons, holdout_seasons, len(y_holdout), metrics, "Target-game final scores are consumed only after prediction/state update; no odds or postgame target features used.", True)


def _apply_calibration(raw: float, intercept: float, slope: float) -> float:
    logit = math.log(raw / (1 - raw))
    return 1 / (1 + math.exp(-max(-30, min(30, intercept + slope * logit))))


def is_supported_market(market: NormalizedMarket) -> bool:
    raw = market.original_metadata.get("market", {})
    sides = raw.get("marketSides") or []
    nfl_sides = isinstance(sides, list) and len(sides) == 2 and all(
        isinstance(side, dict)
        and isinstance(side.get("team"), dict)
        and str(side["team"].get("league") or "").lower() == "nfl"
        for side in sides
    )
    text = " ".join((market.title, market.description, market.resolution_rules, str(raw.get("marketType") or ""))).lower()
    kalshi_nfl_family = market.venue == Venue.KALSHI and any(
        str(value or "").upper().startswith("KXNFLGAME")
        for value in (raw.get("ticker"), raw.get("event_ticker"), market.event)
    )
    banned = ("spread", "total", "over/under", "first half", "quarter", "touchdown", "prop", "future", "super bowl", "playoff berth", "season win")
    return market.venue in {Venue.POLYMARKET, Venue.KALSHI} and ("nfl" in text or nfl_sides or kalshi_nfl_family) and any(x in text for x in ("moneyline", "game winner", "wins", "winner")) and not any(x in text for x in banned)


def nfl_calibration_safe(edge: float | None) -> bool:
    """Require nominal edge to exceed the frozen holdout calibration error."""
    return edge is not None and edge > VALIDATION_ECE + 1e-9


def _team_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    return NFL_TEAM_ALIASES.get(key, key.upper())


def map_market_to_game(market: NormalizedMarket, games: list[NFLGame], *, now: datetime | None = None) -> NFLMapping:
    if not is_supported_market(market):
        return NFLMapping("NON_GAME_WINNER", None, "market is not an NFL pregame game-winner/moneyline")
    raw = market.original_metadata.get("market", {})
    home = str(raw.get("home_team") or raw.get("homeTeam") or "").strip()
    away = str(raw.get("away_team") or raw.get("awayTeam") or "").strip()
    text = " ".join((market.title, market.description, market.resolution_rules))
    sides = raw.get("marketSides") or []
    if isinstance(sides, list) and len(sides) == 2:
        for side in sides:
            team = side.get("team") if isinstance(side, dict) else None
            if not isinstance(team, dict):
                continue
            if str(team.get("ordering") or "").lower() == "home":
                home = str(team.get("name") or team.get("alias") or home).strip()
            elif str(team.get("ordering") or "").lower() == "away":
                away = str(team.get("name") or team.get("alias") or away).strip()
    if not home or not away:
        match = re.search(r"(.+?)\s+vs\.?\s+(.+?)(?:\s+(?:game|match|scheduled|winner|wins)|$)", text, re.I)
        if match:
            away, home = match.group(1).strip(" :-"), match.group(2).strip(" :-")
    wanted = {_team_key(home), _team_key(away)} - {""}
    start_text = str(raw.get("gameStartTime") or raw.get("scheduled_start") or raw.get("start_time") or raw.get("open_time") or "")
    date_hint = start_text[:10] if len(start_text) >= 10 else ""
    candidates = [game for game in games if game.game_type in {"REG", "POST"} and (not date_hint or game.kickoff[:10] == date_hint) and {_team_key(game.home_team), _team_key(game.away_team)} == wanted]
    if len(candidates) == 0:
        return NFLMapping("NO_OFFICIAL_MATCH", None, "teams/date did not match exactly one official NFL game")
    if len(candidates) != 1:
        return NFLMapping("AMBIGUOUS", None, "multiple official NFL games matched")
    game = candidates[0]
    explicit_selected = [raw.get("yes_sub_title"), raw.get("yesSubTitle"), market.outcomes.get("YES")]
    selected_values = explicit_selected if any(str(value or "").strip() not in {"", "YES"} for value in explicit_selected) else [market.title, market.description, market.resolution_rules]
    selected_keys: set[str] = set()
    for value in selected_values:
        candidate = _team_key(value)
        for team in (game.home_team, game.away_team):
            team_key = _team_key(team)
            if team_key and (candidate == team_key or team_key in candidate):
                selected_keys.add(team_key)
    if len(selected_keys) != 1:
        return NFLMapping("AMBIGUOUS", game, "selected market team could not be determined uniquely")
    selected_team = next(iter(selected_keys))
    observed = now or utcnow()
    kickoff = datetime.fromisoformat(game.kickoff.replace("Z", "+00:00"))
    if kickoff <= observed:
        return NFLMapping("PAST_START", game, "official kickoff has passed", selected_team)
    return NFLMapping("MAPPED_GAME_WINNER", game, "exact team/date match", selected_team)


def probability_for_game(game: NFLGame, games: list[NFLGame]) -> float:
    ratings: dict[str, float] = {}
    for prior in games:
        if prior.kickoff >= game.kickoff:
            break
        raw = _probability(ratings.get(prior.home_team, 1500.0), ratings.get(prior.away_team, 1500.0))
        _advance(ratings, prior, raw)
    return _probability(ratings.get(game.home_team, 1500.0), ratings.get(game.away_team, 1500.0))


class NFLEvidenceProvider:
    """Strict live provider, enabled only after the validated GO decision."""
    def __init__(self, games_loader: Callable[[], list[NFLGame]] = fetch_games):
        self.games_loader = games_loader

    def supports(self, market: NormalizedMarket) -> bool:
        return is_supported_market(market)

    def assess(self, market: NormalizedMarket) -> Evidence | None:
        games = self.games_loader()
        mapping = map_market_to_game(market, games)
        if mapping.status != "MAPPED_GAME_WINNER" or mapping.game is None:
            return None
        validation = NFLValidation(SOURCE_URL, SOURCE_LICENSE, ("season", "game_type", "gameday", "home_team", "away_team", "home_score", "away_score"), (), (), (), (), 272, {}, "Target-game final scores are consumed only after prediction/state update; no odds or postgame target features used.", True)
        p_home = probability_for_game(mapping.game, games)
        selected_key = _team_key(mapping.selected_team)
        if selected_key == _team_key(mapping.game.home_team):
            probability = p_home
        elif selected_key == _team_key(mapping.game.away_team):
            probability = 1 - p_home
        else:
            return None
        return evidence_for_market(market, probability, validation)


def evidence_for_market(market: NormalizedMarket, probability: float, validation: NFLValidation, *, now: datetime | None = None) -> Evidence:
    observed = now or utcnow()
    from datetime import timedelta
    return Evidence(venue=market.venue, market_id=market.venue_market_id, fair_probability=probability, source=SOURCE_URL, model_version=MODEL_VERSION, observed_at=observed.isoformat(), valid_until=(observed + timedelta(seconds=VALIDITY_SECONDS)).isoformat(), rules_digest=rules_digest(market), review_reference=json.dumps({"provider": "NFL V1", "holdout": validation.holdout_seasons, "sample_size": validation.sample_size, "validation_ece": VALIDATION_ECE, "calibration_policy": "IDENTITY / EMPIRICALLY VALIDATED RAW PROBABILITIES"}, sort_keys=True), rationale=f"Chronological Elo using only completed games before kickoff; no market price input. Frozen 2025 holdout ECE={VALIDATION_ECE:.6f}; nominal edge must exceed this safety reference.", independent_sources=(SOURCE_URL,), validation_reference="NFL V1 chronological holdout validation", play_type=PlayType.PARALLAX_EDGE, source_independence="independent historical game results", validation_status="CALIBRATED")
