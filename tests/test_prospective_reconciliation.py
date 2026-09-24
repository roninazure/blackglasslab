from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from parallax.cfb import CFBGame
from parallax.demo import demo_inputs
from parallax.models import Side, utcnow
from parallax.nfl import NFLGame
from parallax.normalization import rules_digest
from parallax.prospective_reconciliation import ProspectiveReconciler
from parallax.track_record import TrackRecord


def capture(store, *, verdict="BUY", side=Side.YES, now=None, suffix=""):
    now = now or utcnow() - timedelta(days=2)
    markets, evidence = demo_inputs(now)
    market = replace(markets[0], demo=False, venue_market_id=f"market{suffix}", slug=f"market{suffix}")
    proof = replace(
        evidence[(markets[0].venue, markets[0].venue_market_id)],
        demo=False, market_id=market.venue_market_id, rules_digest=rules_digest(market),
    )
    if verdict == "WATCH":
        market = replace(market, book_timestamp="2020-01-01T00:00:00+00:00")
        proof = replace(proof, rules_digest=rules_digest(market))
    elif verdict == "PASS":
        market = replace(market, status="CLOSED")
        proof = replace(proof, rules_digest=rules_digest(market))
    return store.capture_prospective(market, side, proof, now=now)


@pytest.mark.parametrize("verdict", ["BUY", "WATCH", "PASS"])
@pytest.mark.parametrize("result,winner", [("WIN", "Sample event happens"), ("LOSS", "It does not")])
def test_resolved_win_loss_and_all_verdicts(tmp_path, verdict, result, winner):
    store = TrackRecord(tmp_path / "record.sqlite")
    observation = capture(store, verdict=verdict)
    assert observation["verdict"] == verdict
    assert store.settle_prospective(
        observation["observation_id"], settlement_state="RESOLVED", result=result,
        authoritative_source="fixture", authoritative_source_id="game-1",
        authoritative_winner=winner, settled_at=utcnow().isoformat(), sport="TEST",
    )
    settlement = store.prospective_settlements()[0]
    assert settlement["result"] == result
    assert settlement["frozen_verdict"] == verdict
    assert settlement["binary_outcome"] == int(result == "WIN")
    assert settlement["brier_score"] is not None
    assert settlement["log_loss"] is not None


def test_void_is_supported(tmp_path):
    store = TrackRecord(tmp_path / "record.sqlite")
    observation = capture(store)
    store.settle_prospective(
        observation["observation_id"], settlement_state="VOID", result="VOID",
        authoritative_source="fixture", authoritative_source_id="game-void",
        authoritative_winner=None, settled_at=utcnow().isoformat(), sport="TEST",
    )
    settlement = store.prospective_settlements()[0]
    assert settlement["result"] == "VOID"
    assert settlement["binary_outcome"] is None
    assert settlement["hypothetical_standardized_return_at_frozen_price"] == 0


def test_original_immutable_idempotent_and_conflict_safe(tmp_path):
    store = TrackRecord(tmp_path / "record.sqlite")
    observation = capture(store)
    before = json.dumps(store.prospective_records()[0], sort_keys=True)
    args = {
        "settlement_state": "RESOLVED", "result": "WIN", "authoritative_source": "fixture",
        "authoritative_source_id": "game-1", "authoritative_winner": "Sample event happens",
        "settled_at": utcnow().isoformat(), "sport": "TEST",
    }
    assert store.settle_prospective(observation["observation_id"], **args)
    assert not store.settle_prospective(
        observation["observation_id"], **{**args, "settled_at": utcnow().isoformat()}
    )
    assert len(store.prospective_settlements()) == 1
    assert json.dumps(store.prospective_records()[0], sort_keys=True) == before
    with pytest.raises(ValueError, match="Conflicting"):
        store.settle_prospective(observation["observation_id"], **{**args, "result": "LOSS"})
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError, match="Immutable prospective settlement"):
            db.execute("UPDATE prospective_settlements SET snapshot='{}'")
        with pytest.raises(sqlite3.IntegrityError, match="Immutable prospective settlement"):
            db.execute("DELETE FROM prospective_settlements")


def test_multiple_observations_same_market_keep_frozen_state(tmp_path):
    store = TrackRecord(tmp_path / "record.sqlite")
    now = utcnow() - timedelta(days=2)
    first = capture(store, now=now)
    markets, evidence = demo_inputs(now)
    market = replace(markets[0], demo=False, venue_market_id="market", slug="market", yes_ask=.41)
    proof = replace(
        evidence[(markets[0].venue, markets[0].venue_market_id)], demo=False,
        market_id="market", fair_probability=.70, rules_digest=rules_digest(market),
    )
    second = store.capture_prospective(market, Side.YES, proof, now=now + timedelta(seconds=1))
    for row in (first, second):
        store.settle_prospective(
            row["observation_id"], settlement_state="RESOLVED", result="WIN",
            authoritative_source="fixture", authoritative_source_id="same-game",
            authoritative_winner="Sample event happens", settled_at=utcnow().isoformat(), sport="TEST",
        )
    settlements = store.prospective_settlements()
    assert len(settlements) == 2
    assert {row["authoritative_source_id"] for row in settlements} == {"same-game"}
    assert len({row["frozen_executable_price"] for row in settlements}) == 2
    assert len({row["frozen_model_probability"] for row in settlements}) == 2


def sport_observation(store, sport, *, side=Side.YES, now=None):
    now = now or utcnow() - timedelta(days=2)
    kickoff = now + timedelta(days=1)
    markets, evidence = demo_inputs(now)
    base = markets[0]
    if sport == "MLB":
        away, home, model, ref = "New York Yankees", "Boston Red Sox", "mlb-v2", "official-mlb-statsapi:123"
    elif sport == "NFL":
        away, home, model, ref = "New York Giants", "Dallas Cowboys", "nfl-v1-elo-rolling", "nfl"
    else:
        away, home, model, ref = "Georgia", "Alabama", "cfb-v1-rolling-elo", "cfb"
    raw = {
        "marketType": "moneyline", "gameStartTime": kickoff.isoformat(),
        "home_team": home, "away_team": away,
        "marketSides": [
            {"long": True, "description": away, "team": {"name": away, "safeName": away, "league": sport.lower(), "ordering": "away"}},
            {"long": False, "description": home, "team": {"name": home, "safeName": home, "league": sport.lower(), "ordering": "home"}},
        ],
    }
    if sport == "CFB":
        raw["occurrence_datetime"] = raw.pop("gameStartTime")
    market = replace(
        base, demo=False, venue_market_id=f"{sport}-game", slug=f"{sport}-game",
        title=f"{away} vs {home} {sport} game winner", description=f"{sport} game winner",
        category=sport, outcomes={"YES": away, "NO": home},
        resolution_rules=f"The winner of the {away} vs {home} {sport} game.",
        resolution_time=(kickoff + timedelta(hours=4)).isoformat(),
        original_metadata={"market": raw},
    )
    proof = replace(
        evidence[(base.venue, base.venue_market_id)], demo=False,
        market_id=market.venue_market_id, model_version=model, review_reference=ref,
        rules_digest=rules_digest(market),
    )
    return store.capture_prospective(market, side, proof, now=now), kickoff


def mlb_payload(*, final=True, cancelled=False, duplicate=False):
    game = {
        "gamePk": 123,
        "status": {"abstractGameState": "Final" if final else "Live", "detailedState": "Cancelled" if cancelled else "Final" if final else "In Progress"},
        "teams": {
            "away": {"team": {"name": "New York Yankees"}, "isWinner": True},
            "home": {"team": {"name": "Boston Red Sox"}, "isWinner": False},
        },
    }
    return {"dates": [{"games": [game, dict(game)] if duplicate else [game]}]}


def test_mlb_reconciliation_resolved_void_pending_ambiguous_and_failure(tmp_path):
    cases = [
        (lambda _id: mlb_payload(), "resolved", "WIN"),
        (lambda _id: mlb_payload(final=False), "pending", None),
        (lambda _id: mlb_payload(cancelled=True), "void", "VOID"),
        (lambda _id: mlb_payload(duplicate=True), "ambiguous", None),
    ]
    for index, (loader, status, result) in enumerate(cases):
        store = TrackRecord(tmp_path / f"case-{index}.sqlite")
        observation, _ = sport_observation(store, "MLB")
        decision = ProspectiveReconciler(store, mlb_loader=loader).determine(observation)
        assert decision.status == status and decision.result == result
    store = TrackRecord(tmp_path / "failure.sqlite")
    observation, _ = sport_observation(store, "MLB")
    decision = ProspectiveReconciler(
        store, mlb_loader=lambda _id: (_ for _ in ()).throw(OSError("offline"))
    ).determine(observation)
    assert decision.status == "source_failure" and store.prospective_settlements() == []


def test_nfl_and_cfb_completed_resolution_paths(tmp_path):
    nfl_store = TrackRecord(tmp_path / "nfl.sqlite")
    nfl_observation, nfl_kickoff = sport_observation(nfl_store, "NFL")
    nfl_game = NFLGame("nfl-game-id", nfl_kickoff.year, "REG", nfl_kickoff.isoformat(), "DAL", "NYG", 17, 24)
    nfl_decision = ProspectiveReconciler(nfl_store, nfl_loader=lambda: [nfl_game]).determine(nfl_observation)
    assert nfl_decision.status == "resolved" and nfl_decision.result == "WIN"
    assert nfl_decision.source_id == "nfl-game-id"

    cfb_store = TrackRecord(tmp_path / "cfb.sqlite")
    cfb_observation, cfb_kickoff = sport_observation(cfb_store, "CFB")
    cfb_game = CFBGame(
        "cfb-game-id", cfb_kickoff.year, "regular", cfb_kickoff.isoformat(),
        "Alabama", "Georgia", "1", "2", "fbs", "fbs", False, 21, 24, True,
    )
    cfb_decision = ProspectiveReconciler(
        cfb_store, cfb_loader=lambda _seasons: [cfb_game]
    ).determine(cfb_observation)
    assert cfb_decision.status == "resolved" and cfb_decision.result == "WIN"
    assert cfb_decision.source_id == "cfb-game-id"


def test_unresolved_reconcile_is_pending_and_has_no_side_effect_channels(tmp_path):
    store = TrackRecord(tmp_path / "record.sqlite")
    sport_observation(store, "MLB")
    report = ProspectiveReconciler(
        store, mlb_loader=lambda _id: mlb_payload(final=False),
        clock=lambda: datetime.now(UTC),
    ).reconcile(limit=10)
    assert report["considered"] == 1 and report["settled"] == 0 and report["pending"] == 1
    assert store.pending_prospective()
    assert store.summary()["published_plays"] == 0
    with store.connect() as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("social" in name or "alert" in name or "order" in name for name in tables)
