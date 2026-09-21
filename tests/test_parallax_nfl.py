import importlib.util
import json
from pathlib import Path
from dataclasses import replace
import pytest

from parallax.nfl import NFLGame, NFLEvidenceProvider, VALIDATION_ECE, is_supported_market, map_market_to_game, nfl_calibration_safe, parse_games, validate
from parallax.models import NormalizedMarket, Venue, Mechanics, Side
from parallax.normalization import normalize_pmus
from maker_spread_economics.live_engine import SafetyStop
from maker_spread_economics.polymarket_us import PolymarketUSDiscoveryFailure, PolymarketUSPublicClient

nfl_live_scan_spec = importlib.util.spec_from_file_location(
    "nfl_live_scan", Path(__file__).parents[1] / "scripts" / "nfl_live_scan.py"
)
nfl_live_scan = importlib.util.module_from_spec(nfl_live_scan_spec)
assert nfl_live_scan_spec.loader is not None
nfl_live_scan_spec.loader.exec_module(nfl_live_scan)


def test_parse_nfl_schedule_results_filters_non_games_and_uses_scores_after_parse():
    payload = "season,game_type,gameday,home_team,away_team,home_score,away_score,game_id\n2024,REG,2024-09-08,KC,BAL,27,20,g1\n2024,PRE,2024-08-01,KC,BAL,10,3,g2\n"
    games = parse_games(payload)
    assert len(games) == 1 and games[0].game_id == "g1"


def test_parse_nfl_schedule_retains_current_unresolved_fixture_without_target_result():
    payload = "season,game_type,gameday,home_team,away_team,home_score,away_score,game_id\n2026,REG,2026-09-13,IND,BAL,,,future-1\n"
    games = parse_games(payload)
    assert len(games) == 1
    assert games[0].kickoff == "2026-09-13T04:00:00+00:00"
    assert games[0].home_score is None and games[0].away_score is None


def test_parse_nfl_schedule_uses_existing_local_kickoff_time():
    payload = "season,game_type,gameday,gametime,home_team,away_team,home_score,away_score,game_id\n2026,REG,2026-09-13,13:00,IND,BAL,,,future-2\n"
    assert parse_games(payload)[0].kickoff == "2026-09-13T17:00:00+00:00"


def test_nfl_holdout_is_chronological_and_reproducible():
    games = []
    for season in (2020, 2021, 2022, 2023, 2024, 2025):
        for index in range(40):
            games.append(NFLGame(f"{season}-{index}", season, "REG", f"{season}-09-{index + 1:02d}T12:00:00+00:00", "A", "B", 24 if index % 2 == 0 else 17, 17 if index % 2 == 0 else 24))
    first, second = validate(games, holdout_season=2024), validate(games, holdout_season=2024)
    assert first.train_seasons == (2020,)
    assert first.calibration_seasons == (2021, 2022, 2023)
    assert first.holdout_seasons == (2024,)
    assert first.metrics == second.metrics
    assert "after prediction/state update" in first.leakage_check


def test_nfl_mapping_rejects_derivatives_and_accepts_only_game_winner_shape():
    base = dict(venue=Venue.POLYMARKET, venue_market_id="nfl", slug="nfl", description="NFL rules", category="NFL", event="e", outcomes={"YES":"YES","NO":"NO"}, resolution_rules="NFL game winner", resolution_time="2026-09-20T00:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2026-09-12T00:00:00Z", source_url=None, mechanics=Mechanics())
    assert is_supported_market(NormalizedMarket(title="NFL game winner", **base))
    assert not is_supported_market(NormalizedMarket(title="NFL spread", **{**base, "description":"NFL spread rules"}))


def test_nfl_mapping_is_strict_and_evidence_is_calibrated():
    game = NFLGame("g", 2027, "REG", "2027-09-20T12:00:00+00:00", "KC", "BAL", 0, 0)
    market = NormalizedMarket(title="BAL vs KC NFL game winner", **{**dict(venue=Venue.KALSHI, venue_market_id="nfl", slug="nfl", description="NFL game winner", category="NFL", event="e", outcomes={"YES":"BAL","NO":"KC"}, resolution_rules="NFL game winner", resolution_time="2025-09-20T00:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2025-01-01T00:00:00Z", source_url=None, mechanics=Mechanics()), "original_metadata":{"market":{"away_team":"BAL","home_team":"KC","marketType":"moneyline"}}})
    mapped = map_market_to_game(market, [game], now=__import__("datetime").datetime(2027, 1, 1, tzinfo=__import__("datetime").UTC))
    assert mapped.status == "MAPPED_GAME_WINNER"
    assert NFLEvidenceProvider(lambda: [game]).assess(market).validation_status == "CALIBRATED"


def test_nfl_selected_team_orientation_and_ambiguity(monkeypatch):
    game = NFLGame("g", 2027, "REG", "2027-09-20T12:00:00+00:00", "KC", "BAL", 0, 0)
    base = NormalizedMarket(title="BAL vs KC NFL game winner", venue=Venue.KALSHI, venue_market_id="nfl", slug="nfl", description="NFL game winner", category="NFL", event="e", outcomes={"YES":"BAL","NO":"KC"}, resolution_rules="NFL game winner", resolution_time="2027-09-20T00:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2027-01-01T00:00:00Z", source_url=None, mechanics=Mechanics(), original_metadata={"market":{"away_team":"BAL","home_team":"KC","marketType":"moneyline"}})
    monkeypatch.setattr("parallax.nfl.probability_for_game", lambda *_: .83)
    assert NFLEvidenceProvider(lambda: [game]).assess(base).fair_probability == pytest.approx(.17)
    home = replace(base, outcomes={"YES":"KC","NO":"BAL"})
    assert NFLEvidenceProvider(lambda: [game]).assess(home).fair_probability == pytest.approx(.83)
    ambiguous = replace(base, outcomes={"YES":"YES","NO":"NO"})
    assert NFLEvidenceProvider(lambda: [game]).assess(ambiguous) is None


@pytest.mark.parametrize(
    ("market_id", "slug", "away", "home", "selected", "expected_probability"),
    [
        ("658005", "aec-nfl-nyg-lar-2026-09-21", "New York Giants", "Los Angeles Rams", "New York Giants", .17),
        ("825204", "nfl-sea-was-2026-09-27", "Seattle Seahawks", "Washington Commanders", "Seattle Seahawks", .17),
    ],
)
def test_nfl_pmus_structured_long_side_resolves_real_game_winner_shape(monkeypatch, market_id, slug, away, home, selected, expected_probability):
    raw = {
        "id": market_id, "slug": slug, "question": f"Who will win in the upcoming football event {away} vs {home}?",
        "description": f"This market will settle to the winner of the {away} vs {home} professional football game.", "category": "sports", "eventSlug": slug,
        "active": True, "closed": False, "status": "MARKET_STATUS_OPEN", "ep3Status": "OPEN",
        "accepting_orders": True, "orderPriceMinTickSize": "0.01", "minimumTradeQty": "1",
        "gameStartTime": "2026-09-27T17:00:00Z", "marketType": "moneyline", "sportsMarketType": "football_team_full_game_winner", "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE", "outcomes": json.dumps([away.split()[-1], home.split()[-1]]),
        "marketSides": [
            {"long": selected == away, "description": "YES" if selected == away else "NO", "team": {"league": "nfl", "ordering": "away", "name": away}},
            {"long": selected == home, "description": "YES" if selected == home else "NO", "team": {"league": "nfl", "ordering": "home", "name": home}},
        ],
    }
    market = normalize_pmus({"id": market_id, "slug": slug, "question": raw["question"], "event_id": slug, "active": True, "closed": False, "accepting_orders": True, "raw": raw}, {}, "2026-09-21T00:00:00Z")
    game = NFLGame("g", 2026, "REG", "2026-09-27T17:00:00+00:00", "LA" if home == "Los Angeles Rams" else "WAS", "NYG" if away == "New York Giants" else "SEA", None, None)
    monkeypatch.setattr("parallax.nfl.probability_for_game", lambda *_: .83)
    mapping = map_market_to_game(market, [game], now=__import__("datetime").datetime(2026, 9, 21, tzinfo=__import__("datetime").UTC))
    assert mapping.status == "MAPPED_GAME_WINNER"
    assert mapping.selected_team == (game.home_team if selected == home else game.away_team)
    assert NFLEvidenceProvider(lambda: [game]).assess(market).fair_probability == pytest.approx(expected_probability)


def test_nfl_pmus_structured_long_side_orients_home_team_yes(monkeypatch):
    game = NFLGame("g", 2026, "REG", "2026-09-27T17:00:00+00:00", "WAS", "SEA", None, None)
    raw = {"marketType": "moneyline", "gameStartTime": game.kickoff, "marketSides": [
        {"long": False, "description": "Seahawks", "team": {"league": "nfl", "ordering": "away", "name": "Seattle Seahawks", "alias": "Seahawks", "abbreviation": "sea"}},
        {"long": True, "description": "Commanders", "team": {"league": "nfl", "ordering": "home", "name": "Washington Commanders", "alias": "Commanders", "abbreviation": "was"}},
    ]}
    market = NormalizedMarket(title="Who will win in the upcoming football event Seattle Seahawks vs Washington Commanders?", venue=Venue.POLYMARKET, venue_market_id="pmus-home", slug="pmus-home", description="This market will settle to the winner of the Seattle Seahawks vs Washington Commanders professional football game.", category="sports", event="pmus-home", outcomes={"YES": "YES", "NO": "NO"}, resolution_rules=raw["marketSides"][0]["description"], resolution_time=game.kickoff, status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2026-09-21T00:00:00Z", source_url=None, mechanics=Mechanics(), original_metadata={"market": raw})
    monkeypatch.setattr("parallax.nfl.probability_for_game", lambda *_: .83)
    assert map_market_to_game(market, [game], now=__import__("datetime").datetime(2026, 9, 21, tzinfo=__import__("datetime").UTC)).selected_team == "WAS"
    assert NFLEvidenceProvider(lambda: [game]).assess(market).fair_probability == pytest.approx(.83)


def test_nfl_pmus_multiple_long_sides_remains_ambiguous():
    game = NFLGame("g", 2026, "REG", "2026-09-27T17:00:00+00:00", "LA", "NYG", None, None)
    market = NormalizedMarket(title="NFL game winner", venue=Venue.POLYMARKET, venue_market_id="ambiguous", slug="ambiguous", description="NFL game winner", category="NFL", event="e", outcomes={"YES": "YES", "NO": "NO"}, resolution_rules="NFL game winner", resolution_time="2026-09-27T17:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2026-09-21T00:00:00Z", source_url=None, mechanics=Mechanics(), original_metadata={"market": {"marketType": "moneyline", "gameStartTime": "2026-09-27T17:00:00Z", "marketSides": [{"long": True, "team": {"league": "nfl", "ordering": "away", "name": "New York Giants"}}, {"long": True, "team": {"league": "nfl", "ordering": "home", "name": "Los Angeles Rams"}}]}})
    mapped = map_market_to_game(market, [game], now=__import__("datetime").datetime(2026, 9, 21, tzinfo=__import__("datetime").UTC))
    assert mapped.status == "AMBIGUOUS"
    assert NFLEvidenceProvider(lambda: [game]).assess(market) is None


def test_nfl_calibration_safety_requires_edge_above_frozen_holdout_ece():
    assert not nfl_calibration_safe(0.05)
    assert not nfl_calibration_safe(VALIDATION_ECE)
    assert nfl_calibration_safe(VALIDATION_ECE + 0.001)


def test_nfl_evidence_exposes_frozen_calibration_reference():
    game = NFLGame("g", 2027, "REG", "2027-09-20T12:00:00+00:00", "KC", "BAL", 0, 0)
    market = NormalizedMarket(title="BAL vs KC NFL game winner", **{**dict(venue=Venue.KALSHI, venue_market_id="nfl-meta", slug="nfl-meta", description="NFL game winner", category="NFL", event="e", outcomes={"YES":"BAL","NO":"KC"}, resolution_rules="NFL game winner", resolution_time="2027-09-20T00:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2027-01-01T00:00:00Z", source_url=None, mechanics=Mechanics()), "original_metadata":{"market":{"away_team":"BAL","home_team":"KC","marketType":"moneyline"}}})
    evidence = NFLEvidenceProvider(lambda: [game]).assess(market)
    assert evidence is not None and str(VALIDATION_ECE) in evidence.review_reference


def test_current_kalshi_nfl_game_family_is_supported():
    raw = {
        "ticker": "KXNFLGAME-26SEP13ARILAC-LAC",
        "event_ticker": "KXNFLGAME-26SEP13ARILAC",
    }
    market = NormalizedMarket(
        venue=Venue.KALSHI,
        venue_market_id=raw["ticker"],
        slug=raw["ticker"],
        title="Arizona vs Los Angeles C Pro Football game: Los Angeles C wins?",
        description="Los Angeles C",
        category="Uncategorized",
        event=raw["event_ticker"],
        outcomes={"YES": "YES", "NO": "NO"},
        resolution_rules="If Los Angeles C wins the Arizona vs Los Angeles C professional football game, then the market resolves to Yes.",
        resolution_time="2026-09-14T02:25:00Z",
        status="OPEN",
        yes_bid=.4,
        yes_ask=.5,
        no_bid=.4,
        no_ask=.5,
        best_bid_size=1,
        best_ask_size=1,
        executable_depth={},
        recent_volume=None,
        recent_trade_count=None,
        last_trade_time=None,
        book_timestamp=None,
        data_timestamp="2026-09-13T21:00:00Z",
        source_url=None,
        mechanics=Mechanics(),
        original_metadata={"market": raw},
    )
    assert is_supported_market(market)


def test_nfl_evaluated_play_reaches_prospective_capture(monkeypatch):
    captured = []
    from types import SimpleNamespace
    sentinel = SimpleNamespace(id="play", venue="PMUS", market_id="market", side=Side.YES)
    monkeypatch.setattr(nfl_live_scan, "qualify", lambda *args, **kwargs: sentinel)

    class Store:
        def capture_prospective(self, *args, **kwargs):
            captured.append((args, kwargs))
            return {"observation_id": "PX-1", "play_id": "play", "venue": "PMUS", "market_id": "market", "side": Side.YES}

        def prospective_record(self, observation_id):
            return {"observation_id": observation_id}

    result = nfl_live_scan._capture_evaluated(Store(), object(), Side.YES, object(), "now")
    assert result is sentinel and len(captured) == 1


def test_nfl_capture_failure_is_fail_closed(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(nfl_live_scan, "qualify", lambda *args, **kwargs: SimpleNamespace(id="p", venue="PMUS", market_id="m", side=Side.YES))

    class Store:
        def capture_prospective(self, *args, **kwargs):
            raise RuntimeError("disk failure")

    import pytest
    with pytest.raises(RuntimeError):
        nfl_live_scan._capture_evaluated(Store(), object(), Side.YES, object(), "now")


def test_nfl_capture_missing_or_mismatched_observation_is_fail_closed(monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(nfl_live_scan, "qualify", lambda *args, **kwargs: SimpleNamespace(id="p", venue="PMUS", market_id="m", side=Side.YES))

    class Store:
        def capture_prospective(self, *args, **kwargs):
            return {"play_id": "different", "venue": "PMUS", "market_id": "m", "side": Side.YES}

        def prospective_record(self, observation_id):
            return None

    import pytest
    with pytest.raises(ValueError, match="verified durable observation ID"):
        nfl_live_scan._capture_evaluated(Store(), object(), Side.YES, object(), "now")


def test_pmus_discovery_retries_timeout_then_succeeds(monkeypatch):
    class TimeoutErrorFromSDK(Exception):
        pass
    class Markets:
        def __init__(self): self.calls = 0
        def list(self, _params):
            self.calls += 1
            if self.calls < 3:
                raise TimeoutErrorFromSDK("read timeout")
            return {"markets": []}
    class Client:
        def __init__(self): self.markets = Markets()
    sleeps = []
    monkeypatch.setattr("maker_spread_economics.polymarket_us.time.sleep", sleeps.append)
    client = Client()
    assert PolymarketUSPublicClient(client=client).markets_page(limit=100, offset=0) == []
    assert client.markets.calls == 3 and sleeps == [0.1, 0.2]


def test_pmus_discovery_exhaustion_is_fail_closed(monkeypatch):
    class APITimeoutError(Exception): pass
    class Markets:
        def list(self, _params): raise APITimeoutError("read timeout")
    class Client:
        markets = Markets()
    monkeypatch.setattr("maker_spread_economics.polymarket_us.time.sleep", lambda _seconds: None)
    with pytest.raises(PolymarketUSDiscoveryFailure) as caught:
        PolymarketUSPublicClient(client=Client()).markets_page(limit=100, offset=0)
    assert caught.value.attempts == 3
    assert isinstance(caught.value, SafetyStop)


def test_pmus_discovery_non_retryable_safetystop_is_not_retried(monkeypatch):
    class Markets:
        def list(self, _params): raise SafetyStop("venue closed-only")
    class Client:
        markets = Markets()
    sleeps = []
    monkeypatch.setattr("maker_spread_economics.polymarket_us.time.sleep", sleeps.append)
    with pytest.raises(SafetyStop, match="closed-only"):
        PolymarketUSPublicClient(client=Client()).markets_page(limit=100, offset=0)
    assert sleeps == []
