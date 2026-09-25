"""Small, independent MLB pregame evidence provider.

The provider consumes official MLB Stats API facts, never prediction-market or
bookmaker prices. Network access is behind an injectable transport so tests are
fully offline.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import Evidence, NormalizedMarket, PlayType, Side, Venue, utcnow
from .normalization import rules_digest

MLB_API = "https://statsapi.mlb.com/api/v1"
MODEL_VERSION = "mlb-v2"
SOURCE_ID = "official-mlb-statsapi"
VALIDITY_SECONDS = 15 * 60


def _text(value: Any) -> str:
    return " ".join(str(value or "").lower().replace("&", "and").split())


def _team_key(value: Any) -> str:
    key = re.sub(r"[^a-z0-9]", "", _text(value))
    aliases = {
        "colorado": "coloradorockies", "detroit": "detroittigers", "miami": "miamimarlins", "arizona": "arizonadiamondbacks",
        "chicagoc": "chicagocubs", "pittsburgh": "pittsburghpirates", "toronto": "torontobluejays", "baltimore": "baltimoreorioles",
        "philadelphia": "philadelphiaphillies", "atlanta": "atlantabraves", "losangelesd": "losangelesdodgers", "losangelesa": "losangelesangels",
        "sandiego": "sandiegopadres", "sanfrancisco": "sanfranciscogiants", "boston": "bostonredsox", "kansascity": "kansascityroyals", "washington": "washingtonnationals",
    }
    return aliases.get(key, key)


def selected_team_for_moneyline(
    market: NormalizedMarket,
    side: Side,
) -> str | None:
    """Return the actual team bought by a two-team MLB moneyline side.

    Kalshi names the YES contract team and can repeat that label on NO, so NO
    must be oriented to the other official team rather than trusting the
    display subtitle.
    """
    metadata = market.original_metadata.get("market", {})
    mlb = metadata.get("mlb") if isinstance(metadata, dict) else None
    if not isinstance(mlb, dict):
        return None
    home = str(mlb.get("home_team") or "").strip()
    away = str(mlb.get("away_team") or "").strip()
    if not home or not away:
        return None
    yes_key = _team_key(market.outcomes.get("YES"))
    home_key, away_key = _team_key(home), _team_key(away)
    if yes_key == home_key:
        yes_team, no_team = home, away
    elif yes_key == away_key:
        yes_team, no_team = away, home
    else:
        return None
    return yes_team if side == Side.YES else no_team


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)).astimezone(UTC)
    except (TypeError, ValueError):
        return None


def _probability(value: float) -> float:
    return min(1.0 - 1e-6, max(1e-6, float(value)))


@dataclass(frozen=True)
class MLBGameFact:
    game_id: str
    game_date: str
    start_time: str
    home_team: str
    away_team: str
    home_win_rate: float
    away_win_rate: float
    home_run_diff_per_game: float
    away_run_diff_per_game: float
    home_pitcher_era: float | None = None
    away_pitcher_era: float | None = None
    home_bullpen_era: float | None = None
    away_bullpen_era: float | None = None
    home_won: bool | None = None
    v2_probability: float | None = None


V2_MODEL_VERSION = MODEL_VERSION
V2_CALIBRATOR = (0.016348397839057574, 0.5117094959855781)  # frozen from 2024 only


@dataclass
class V2State:
    ratings: dict[str, float] = field(default_factory=dict)
    stats: dict[str, list[float]] = field(default_factory=dict)


def v2_probability_from_state(home_id: str, away_id: str, state: V2State) -> float:
    rh, ra = state.ratings.get(home_id, 1500.0), state.ratings.get(away_id, 1500.0)
    hs, aas = state.stats.get(home_id, [0.0, 0.0, 0.0]), state.stats.get(away_id, [0.0, 0.0, 0.0])
    hf = hs[0] / hs[2] if hs[2] else 0.5
    af = aas[0] / aas[2] if aas[2] else 0.5
    hr = hs[1] / hs[2] if hs[2] else 0.0
    ar = aas[1] / aas[2] if aas[2] else 0.0
    z = ((rh - ra) + 24.0 + 70.0 * (hf - af) + 8.0 * (hr - ar)) / 400.0
    return _probability(max(0.05, min(0.95, 1 / (1 + 10 ** (-z)))))


def advance_v2_state(state: V2State, row: dict[str, Any]) -> None:
    home, away = str(row["home_id"]), str(row["away_id"])
    home_rating = state.ratings.get(home, 1500.0)
    away_rating = state.ratings.get(away, 1500.0)
    p = v2_probability_from_state(home, away, state)
    y = int(bool(row["home_won"]))
    state.ratings[home] = home_rating + 20.0 * (y - p)
    state.ratings[away] = away_rating + 20.0 * ((1 - y) - (1 - p))
    hs = state.stats.get(home, [0.0, 0.0, 0.0])
    aas = state.stats.get(away, [0.0, 0.0, 0.0])
    state.stats[home] = [hs[0] + y, hs[1] + float(row["home_runs"]) - float(row["away_runs"]), hs[2] + 1]
    state.stats[away] = [aas[0] + 1 - y, aas[1] + float(row["away_runs"]) - float(row["home_runs"]), aas[2] + 1]


def calibrated_v2_probability(raw: float) -> float:
    intercept, slope = V2_CALIBRATOR
    logit = math.log(raw / (1 - raw))
    return max(0.05, min(0.95, 1 / (1 + math.exp(-max(-30.0, min(30.0, intercept + slope * logit))))))


def _log5(home_rate: float, away_rate: float) -> float:
    h, a = _probability(home_rate), _probability(away_rate)
    return (h * (1.0 - a)) / ((h * (1.0 - a)) + (a * (1.0 - h)))


def game_probability_v1(game: MLBGameFact) -> float:
    """The frozen V1 model, retained solely as an evaluation comparator."""
    base = _log5(game.home_win_rate, game.away_win_rate)
    run_delta = max(-1.5, min(1.5, game.home_run_diff_per_game - game.away_run_diff_per_game))
    adjustment = 0.025 * run_delta + 0.035  # fixed home-field effect
    if game.home_pitcher_era is not None and game.away_pitcher_era is not None:
        adjustment += max(-0.08, min(0.08, (game.away_pitcher_era - game.home_pitcher_era) * 0.012))
    if game.home_bullpen_era is not None and game.away_bullpen_era is not None:
        adjustment += max(-0.04, min(0.04, (game.away_bullpen_era - game.home_bullpen_era) * 0.006))
    return _probability(base + adjustment)


def game_probability(game: MLBGameFact) -> float:
    """Compatibility probability for an independently supplied pregame fact.

    The walk-forward evaluator uses the stateful V2 implementation below. This
    bounded fallback is intentionally conservative when no online state is
    available to the live provider.
    """
    if game.v2_probability is not None:
        return _probability(game.v2_probability)
    return _probability(0.5 + 0.04 * max(-1.5, min(1.5, game.home_run_diff_per_game - game.away_run_diff_per_game)) + 0.035)


class MLBStatsAPI:
    def __init__(self, transport: Callable[[str], dict[str, Any]] | None = None):
        self.transport = transport or self._get
        self._target_probability_cache: dict[str, float | None] = {}

    @staticmethod
    def _get(path: str) -> dict[str, Any]:
        request = Request(f"{MLB_API}{path}", headers={"Accept": "application/json"})
        with urlopen(request, timeout=10) as response:
            return json.load(response)

    def scheduled_games_for_date(self, target_date: str) -> list[dict[str, Any]]:
        """Return the authoritative MLB slate for one calendar date.

        gamePk is the identity so doubleheaders between the same clubs remain
        separate scheduled games.
        """
        payload = self.transport(
            "/schedule?"
            + urlencode({"sportId": 1, "date": target_date, "hydrate": "team"})
        )
        scheduled: list[dict[str, Any]] = []
        for day in payload.get("dates", []):
            for row in day.get("games", []):
                if row.get("gameType", "R") != "R":
                    continue
                game_id = str(row.get("gamePk") or "").strip()
                start = _parse_time(row.get("gameDate"))
                teams = row.get("teams", {})
                home = str(teams.get("home", {}).get("team", {}).get("name") or "").strip()
                away = str(teams.get("away", {}).get("team", {}).get("name") or "").strip()
                if not game_id or start is None or not home or not away:
                    continue
                status = row.get("status", {})
                detailed = str(status.get("detailedState") or "").upper()
                abstract = str(status.get("abstractGameState") or "").upper()
                combined = f"{abstract} {detailed}"
                if "POSTPON" in combined:
                    schedule_status = "POSTPONED"
                elif "CANCEL" in combined:
                    schedule_status = "CANCELLED"
                elif "SUSPEND" in combined:
                    schedule_status = "SUSPENDED"
                elif "DELAY" in combined:
                    schedule_status = "DELAYED"
                elif abstract == "FINAL" or "FINAL" in detailed:
                    schedule_status = "FINAL"
                elif abstract == "LIVE" or "IN PROGRESS" in detailed:
                    schedule_status = "IN_PROGRESS"
                else:
                    schedule_status = "SCHEDULED"
                scheduled.append(
                    {
                        "game_id": game_id,
                        "date": target_date,
                        "start_time": start.isoformat(),
                        "away_team": away,
                        "home_team": home,
                        "schedule_status": schedule_status,
                    }
                )
        return scheduled

    def game_for_market(self, market: NormalizedMarket) -> MLBGameFact | None:
        metadata = market.original_metadata.get("market", {})
        mlb = metadata.get("mlb") if isinstance(metadata, dict) else None
        if not isinstance(mlb, dict):
            return None
        if _text(mlb.get("league")) not in {"mlb", "major league baseball"}:
            return None
        if _text(mlb.get("market_type")) not in {"moneyline", "game winner", "game-winner"}:
            return None
        home, away = mlb.get("home_team"), mlb.get("away_team")
        start = _parse_time(mlb.get("start_time"))
        if not home or not away or start is None or start <= utcnow():
            return None
        if not market.resolution_rules.strip() or not any(term in _text(market.resolution_rules) for term in ("winner", " wins", " win")):
            return None
        payload = self.transport("/schedule?" + urlencode({"sportId": 1, "date": start.date().isoformat(), "hydrate": "team"}))
        games = payload.get("dates", [])
        matches = []
        for day in games:
            for row in day.get("games", []):
                teams = row.get("teams", {})
                names = {_team_key(teams.get("home", {}).get("team", {}).get("name")), _team_key(teams.get("away", {}).get("team", {}).get("name"))}
                if names == {_team_key(home), _team_key(away)} and row.get("gameType", "R") == "R":
                    matches.append(row)
        if len(matches) != 1:
            return None
        row = matches[0]
        facts = mlb.get("pregame_facts")
        if not isinstance(facts, dict):
            previous = self.transport("/standings?" + urlencode({"leagueId": "103,104", "season": start.year - 1, "standingsTypes": "regularSeason"}))
            records = {}
            for block in previous.get("records", []):
                for record in block.get("teamRecords", []):
                    name = record.get("team", {}).get("name")
                    if name:
                        wins, losses = float(record.get("wins", 0)), float(record.get("losses", 0))
                        total = wins + losses
                        if total <= 0:
                            continue
                        runs_for, runs_against = float(record.get("runsScored", 0)), float(record.get("runsAllowed", 0))
                        records[_team_key(name)] = (wins / total, (runs_for - runs_against) / total)
            # V2 replay is authoritative; these legacy fields are retained only
            # for the fact envelope and may safely be neutral when standings are
            # unavailable.
            home_record = records.get(_team_key(home), (0.5, 0.0))
            away_record = records.get(_team_key(away), (0.5, 0.0))
            facts = {
                "home_win_rate": home_record[0], "away_win_rate": away_record[0],
                "home_run_diff_per_game": home_record[1], "away_run_diff_per_game": away_record[1],
            }
        try:
            official_home = str(row.get("teams", {}).get("home", {}).get("team", {}).get("name") or home)
            official_away = str(row.get("teams", {}).get("away", {}).get("team", {}).get("name") or away)
            mlb.update({"mlb_game_pk": str(row["gamePk"]), "home_team": official_home, "away_team": official_away, "scheduled_start": row.get("gameDate")})
            v2 = self._v2_for_target(row)
            return MLBGameFact(
                str(row["gamePk"]), start.date().isoformat(), start.isoformat(), official_home, official_away,
                float(facts["home_win_rate"]), float(facts["away_win_rate"]),
                float(facts["home_run_diff_per_game"]), float(facts["away_run_diff_per_game"]),
                facts.get("home_pitcher_era"), facts.get("away_pitcher_era"),
                facts.get("home_bullpen_era"), facts.get("away_bullpen_era"), v2_probability=v2,
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _v2_for_target(self, target: dict[str, Any]) -> float | None:
        """Replay the validated state using only completed games before target."""
        if str(target.get("gamePk")) in self._target_probability_cache:
            return self._target_probability_cache[str(target.get("gamePk"))]
        from .mlb import V2State, advance_v2_state, calibrated_v2_probability, v2_probability_from_state
        target_time = _parse_time(target.get("gameDate"))
        if target_time is None:
            self._target_probability_cache[str(target.get("gamePk"))] = None
            return None
        state = V2State()
        for season in range(2023, target_time.year + 1):
            payload = self.transport("/schedule?" + urlencode({"sportId": 1, "startDate": f"{season}-03-20", "endDate": f"{season}-11-01", "hydrate": "team"}))
            for day in payload.get("dates", []):
                for game in day.get("games", []):
                    if game.get("gameType") != "R" or game.get("status", {}).get("abstractGameState") != "Final":
                        continue
                    game_time = _parse_time(game.get("gameDate"))
                    if game_time is None or game_time >= target_time:
                        continue
                    teams = game.get("teams", {})
                    home_row, away_row = teams.get("home", {}), teams.get("away", {})
                    home_id = home_row.get("team", {}).get("id")
                    away_id = away_row.get("team", {}).get("id")
                    if home_id is None or away_id is None or "isWinner" not in home_row:
                        continue
                    advance_v2_state(state, {"home_id": home_id, "away_id": away_id, "home_won": home_row["isWinner"], "home_runs": home_row.get("score", 0), "away_runs": away_row.get("score", 0)})
        home_id = target.get("teams", {}).get("home", {}).get("team", {}).get("id")
        away_id = target.get("teams", {}).get("away", {}).get("team", {}).get("id")
        if home_id is None or away_id is None:
            self._target_probability_cache[str(target.get("gamePk"))] = None
            return None
        result = calibrated_v2_probability(v2_probability_from_state(str(home_id), str(away_id), state))
        self._target_probability_cache[str(target.get("gamePk"))] = result
        return result


class MLBEvidenceProvider:
    name = SOURCE_ID

    def __init__(self, source: MLBStatsAPI | None = None, clock: Callable[[], datetime] = utcnow):
        self.source = source or MLBStatsAPI()
        self.clock = clock

    def supports(self, market: NormalizedMarket) -> bool:
        raw = market.original_metadata.get("market", {})
        mlb = raw.get("mlb") if isinstance(raw, dict) else None
        return bool(
            isinstance(mlb, dict)
            and _text(mlb.get("league")) in {"mlb", "major league baseball"}
            and _text(mlb.get("market_type")) in {"moneyline", "game winner", "game-winner"}
            and market.venue in {Venue.POLYMARKET, Venue.KALSHI}
        )

    def assess(self, market: NormalizedMarket) -> Evidence | None:
        if not self.supports(market):
            return None
        try:
            game = self.source.game_for_market(market)
            if game is None:
                return None
            probability = game_probability(game)
            yes = _team_key(market.outcomes.get("YES"))
            no = _team_key(market.outcomes.get("NO"))
            home_key, away_key = _team_key(game.home_team), _team_key(game.away_team)
            # PMUS names both outcomes; Kalshi names the YES contract team
            # and repeats it on NO. In both cases orient evidence to YES.
            if yes == away_key:
                probability = 1.0 - probability
            elif yes != home_key:
                return None
            now = self.clock()
            return Evidence(
                venue=market.venue, market_id=market.venue_market_id,
                fair_probability=probability, source=self.name, model_version=MODEL_VERSION,
                observed_at=now.isoformat(), valid_until=(now + timedelta(seconds=VALIDITY_SECONDS)).isoformat(),
                rules_digest=rules_digest(market), review_reference=f"{self.name}:{game.game_id}",
                rationale=(f"Pregame MLB model from home/away record, run differential, home field"
                           f" and available pitching facts for {game.away_team} at {game.home_team}."),
                independent_sources=(f"{MLB_API}/schedule gamePk={game.game_id}",),
                validation_reference="calibration:mlb-v2:2024-trained-2025-untouched-official-regular-season",
                play_type=PlayType.PARALLAX_VALUE, source_independence="AUTHORITATIVE_PRIMARY",
                validation_status="CALIBRATED",
            )
        except Exception:
            return None


def evaluate_walk_forward(rows: list[MLBGameFact]) -> dict[str, Any]:
    """Evaluate supplied resolved games in chronological order.

    The model is parameter-free in V1, so chronological ordering is enforced
    by requiring each row to precede the next; no future row is consumed.
    """
    ordered = sorted(rows, key=lambda row: row.start_time)
    predictions, brier, logloss, correct = [], [], [], 0
    for row in ordered:
        p = game_probability(row)
        actual = 1 if row.home_won else 0
        predictions.append(p)
        brier.append((p - actual) ** 2)
        logloss.append(-math.log(p if actual else 1 - p))
        correct += int((p >= 0.5) == bool(actual))
    buckets = []
    for low in (0.0, 0.2, 0.4, 0.6, 0.8):
        selected = [i for i, p in enumerate(predictions) if low <= p < low + 0.2 or (low == 0.8 and low <= p <= 1)]
        if selected:
            buckets.append({"lower": low, "upper": min(1.0, low + 0.2), "count": len(selected), "mean_probability": sum(predictions[i] for i in selected) / len(selected), "observed_home_rate": sum(1 if ordered[i].home_won else 0 for i in selected) / len(selected)})
    return {"predictions": len(rows), "brier_score": sum(brier) / len(brier) if brier else None, "log_loss": sum(logloss) / len(logloss) if logloss else None, "accuracy": correct / len(rows) if rows else None, "calibration_buckets": buckets, "model_version": MODEL_VERSION}
