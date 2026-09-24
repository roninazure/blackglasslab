"""Bounded, read-only-source reconciliation for prospective sports observations."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlencode

from . import cfb, nfl
from .mlb import MLB_API, MLBStatsAPI
from .mlb import _team_key as mlb_team_key
from .models import Mechanics, NormalizedMarket, Venue, timestamp, utcnow
from .track_record import TrackRecord


@dataclass(frozen=True)
class ResolutionDecision:
    status: str
    reason: str
    sport: str | None = None
    settlement_state: str | None = None
    result: str | None = None
    winner: str | None = None
    source: str | None = None
    source_id: str | None = None
    source_resolved_at: str | None = None


def _market(snapshot: dict[str, Any]) -> NormalizedMarket:
    raw = dict(snapshot["market_snapshot"])
    raw["venue"] = Venue(raw["venue"])
    raw["mechanics"] = Mechanics(**raw["mechanics"])
    raw["executable_depth"] = {
        key: tuple(tuple(level) for level in levels)
        for key, levels in raw.get("executable_depth", {}).items()
    }
    return NormalizedMarket(**raw)


def _selected_team_keys(
    snapshot: dict[str, Any], canonical: Callable[[Any], str]
) -> set[str]:
    selected = str(snapshot.get("selected_outcome") or "").strip()
    selected_side = snapshot.get("side")
    raw = snapshot.get("market_snapshot", {}).get("original_metadata", {}).get("market", {})
    sides = raw.get("marketSides") if isinstance(raw, dict) else None
    candidates = [selected]
    if isinstance(sides, list):
        matched = []
        for side in sides:
            if not isinstance(side, dict):
                continue
            description = str(side.get("description") or "").strip()
            side_name = "YES" if side.get("long") is True else "NO" if side.get("long") is False else None
            if description.casefold() == selected.casefold() or side_name == selected_side:
                matched.append(side)
        if len(matched) == 1:
            matched_side = matched[0]
            team = matched_side.get("team")
            if isinstance(team, dict):
                candidates = [
                    team.get("safeName"), team.get("name"), team.get("alias"),
                    team.get("abbreviation"), matched_side.get("description"), selected,
                ]
    return {canonical(value) for value in candidates if value}


def _result_for_selected(
    snapshot: dict[str, Any], *, home: str, away: str, winner: str,
    canonical: Callable[[Any], str],
) -> str | None:
    selected_candidates = _selected_team_keys(snapshot, canonical)
    home_key, away_key, winner_key = canonical(home), canonical(away), canonical(winner)
    selected = selected_candidates & {home_key, away_key}
    if len(selected) != 1 or winner_key not in {home_key, away_key}:
        return None
    return "WIN" if next(iter(selected)) == winner_key else "LOSS"


class ProspectiveReconciler:
    """Resolve only exact, completed sport identities from existing source clients."""

    def __init__(
        self,
        store: TrackRecord,
        *,
        mlb_loader: Callable[[str], dict[str, Any]] | None = None,
        nfl_loader: Callable[[], list[nfl.NFLGame]] = nfl.fetch_games,
        cfb_loader: Callable[[tuple[int, ...]], list[cfb.CFBGame]] = cfb.fetch_games,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.store = store
        self.mlb_loader = mlb_loader or self._load_mlb
        self.nfl_loader = nfl_loader
        self.cfb_loader = cfb_loader
        self.clock = clock
        self._mlb_cache: dict[str, dict[str, Any]] = {}
        self._nfl_games: list[nfl.NFLGame] | None = None
        self._cfb_games: dict[tuple[int, ...], list[cfb.CFBGame]] = {}

    @staticmethod
    def _load_mlb(game_id: str) -> dict[str, Any]:
        path = "/schedule?" + urlencode({"sportId": 1, "gamePk": game_id, "hydrate": "team"})
        return MLBStatsAPI().transport(path)

    def determine(self, observation: dict[str, Any]) -> ResolutionDecision:
        model = str(observation.get("evidence_snapshot", {}).get("model_version") or "")
        try:
            if model.startswith("mlb-"):
                return self._mlb(observation)
            if model.startswith("nfl-"):
                return self._nfl(observation)
            if model.startswith("cfb-"):
                return self._cfb(observation)
        except Exception as exc:  # noqa: BLE001 - one source failure cannot settle a row
            return ResolutionDecision("source_failure", type(exc).__name__)
        return ResolutionDecision("unsupported", "unsupported prospective sport")

    def _mlb(self, observation: dict[str, Any]) -> ResolutionDecision:
        reference = str(observation.get("evidence_snapshot", {}).get("review_reference") or "")
        prefix = "official-mlb-statsapi:"
        if not reference.startswith(prefix) or not reference[len(prefix):].isdigit():
            return ResolutionDecision("ambiguous", "missing exact MLB gamePk", sport="MLB")
        game_id = reference[len(prefix):]
        if game_id not in self._mlb_cache:
            self._mlb_cache[game_id] = self.mlb_loader(game_id)
        games = [
            game for day in self._mlb_cache[game_id].get("dates", [])
            for game in day.get("games", []) if str(game.get("gamePk")) == game_id
        ]
        if len(games) != 1:
            return ResolutionDecision("ambiguous", "gamePk did not return exactly one game", sport="MLB")
        game = games[0]
        state = str(game.get("status", {}).get("detailedState") or "").casefold()
        if state in {"cancelled", "canceled"}:
            return ResolutionDecision(
                "void", "official game cancellation", "MLB", "VOID", "VOID", None,
                "official-mlb-statsapi", game_id,
            )
        if str(game.get("status", {}).get("abstractGameState")) != "Final":
            return ResolutionDecision("pending", "official game is not final", sport="MLB")
        teams = game.get("teams", {})
        home, away = teams.get("home", {}), teams.get("away", {})
        home_name = str(home.get("team", {}).get("name") or "")
        away_name = str(away.get("team", {}).get("name") or "")
        winners = [name for row, name in ((home, home_name), (away, away_name)) if row.get("isWinner") is True]
        if len(winners) != 1:
            return ResolutionDecision("ambiguous", "final MLB winner is not unique", sport="MLB")
        result = _result_for_selected(
            observation, home=home_name, away=away_name, winner=winners[0], canonical=mlb_team_key
        )
        if result is None:
            return ResolutionDecision("ambiguous", "selected MLB team did not map uniquely", sport="MLB")
        return ResolutionDecision(
            "resolved", "exact gamePk final", "MLB", "RESOLVED", result, winners[0],
            "official-mlb-statsapi", game_id,
        )

    def _nfl(self, observation: dict[str, Any]) -> ResolutionDecision:
        if self._nfl_games is None:
            self._nfl_games = self.nfl_loader()
        market = _market(observation)
        captured = timestamp(observation.get("captured_at"))
        mapping = nfl.map_market_to_game(market, self._nfl_games, now=captured)
        if mapping.status != "MAPPED_GAME_WINNER" or mapping.game is None:
            status = "ambiguous" if mapping.status in {"AMBIGUOUS", "NO_OFFICIAL_MATCH"} else "unsupported"
            return ResolutionDecision(status, mapping.reason, sport="NFL")
        game = mapping.game
        if game.home_score is None or game.away_score is None:
            return ResolutionDecision("pending", "official NFL result is incomplete", sport="NFL")
        if game.home_score == game.away_score:
            return ResolutionDecision("pending", "tie requires venue resolution", sport="NFL")
        winner = game.home_team if game.home_score > game.away_score else game.away_team
        result = _result_for_selected(
            observation, home=game.home_team, away=game.away_team, winner=winner,
            canonical=nfl._team_key,
        )
        if result is None:
            return ResolutionDecision("ambiguous", "selected NFL team did not map uniquely", sport="NFL")
        return ResolutionDecision(
            "resolved", "exact team/date completed result", "NFL", "RESOLVED", result,
            winner, nfl.SOURCE_URL, game.game_id,
        )

    def _cfb(self, observation: dict[str, Any]) -> ResolutionDecision:
        market = _market(observation)
        metadata = cfb._cfb_metadata(market)
        start = timestamp(metadata.get("start_time")) if metadata else None
        if start is None:
            return ResolutionDecision("ambiguous", "missing CFB season identity", sport="CFB")
        seasons = (start.year,)
        if seasons not in self._cfb_games:
            self._cfb_games[seasons] = self.cfb_loader(seasons)
        captured = timestamp(observation.get("captured_at"))
        mapping = cfb.map_market_to_game(market, self._cfb_games[seasons], now=captured)
        if mapping.status != "MAPPED" or mapping.game is None:
            status = "ambiguous" if mapping.status in {"TEAM_PAIR_MISMATCH", "DATE_MISMATCH", "AMBIGUOUS_ALIAS"} else "unsupported"
            return ResolutionDecision(status, mapping.reason, sport="CFB")
        game = mapping.game
        if not game.completed or game.home_score is None or game.away_score is None:
            return ResolutionDecision("pending", "authoritative CFB result is incomplete", sport="CFB")
        if game.home_score == game.away_score:
            return ResolutionDecision("pending", "tie requires venue resolution", sport="CFB")
        winner = game.home_team if game.home_score > game.away_score else game.away_team
        result = _result_for_selected(
            observation, home=game.home_team, away=game.away_team, winner=winner,
            canonical=cfb._canonical_team,
        )
        if result is None:
            return ResolutionDecision("ambiguous", "selected CFB team did not map uniquely", sport="CFB")
        return ResolutionDecision(
            "resolved", "exact FBS team/date/orientation completed result", "CFB", "RESOLVED",
            result, winner, cfb.SOURCE_URL, game.game_id,
        )

    def reconcile(self, *, limit: int = 500) -> dict[str, int]:
        observations = self.store.pending_prospective(limit=limit)
        counts: Counter[str] = Counter(considered=len(observations))
        settled_at = self.clock().astimezone(UTC).isoformat()
        for observation in observations:
            decision = self.determine(observation)
            counts[decision.status] += 1
            if decision.settlement_state is None or decision.result is None:
                counts["pending"] += int(decision.status != "pending")
                continue
            inserted = self.store.settle_prospective(
                observation["observation_id"],
                settlement_state=decision.settlement_state,
                result=decision.result,
                authoritative_source=str(decision.source),
                authoritative_source_id=str(decision.source_id),
                authoritative_winner=decision.winner,
                settled_at=settled_at,
                source_resolved_at=decision.source_resolved_at,
                sport=decision.sport,
            )
            counts["settled"] += int(inserted)
        for key in ("considered", "settled", "resolved", "void", "pending", "ambiguous", "source_failure", "unsupported"):
            counts.setdefault(key, 0)
        return dict(counts)


__all__ = ["MLB_API", "ProspectiveReconciler", "ResolutionDecision"]
