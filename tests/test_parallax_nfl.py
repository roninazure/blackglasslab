from parallax.nfl import NFLGame, NFLEvidenceProvider, VALIDATION_ECE, is_supported_market, map_market_to_game, nfl_calibration_safe, parse_games, validate
from parallax.models import NormalizedMarket, Venue, Mechanics


def test_parse_nfl_schedule_results_filters_non_games_and_uses_scores_after_parse():
    payload = "season,game_type,gameday,home_team,away_team,home_score,away_score,game_id\n2024,REG,2024-09-08,KC,BAL,27,20,g1\n2024,PRE,2024-08-01,KC,BAL,10,3,g2\n"
    games = parse_games(payload)
    assert len(games) == 1 and games[0].game_id == "g1"


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
    market = NormalizedMarket(title="BAL vs KC NFL game winner", **{**dict(venue=Venue.KALSHI, venue_market_id="nfl", slug="nfl", description="NFL game winner", category="NFL", event="e", outcomes={"YES":"YES","NO":"NO"}, resolution_rules="NFL game winner", resolution_time="2025-09-20T00:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2025-01-01T00:00:00Z", source_url=None, mechanics=Mechanics()), "original_metadata":{"market":{"away_team":"BAL","home_team":"KC","marketType":"moneyline"}}})
    mapped = map_market_to_game(market, [game], now=__import__("datetime").datetime(2027, 1, 1, tzinfo=__import__("datetime").UTC))
    assert mapped.status == "MAPPED_GAME_WINNER"
    assert NFLEvidenceProvider(lambda: [game]).assess(market).validation_status == "CALIBRATED"


def test_nfl_calibration_safety_requires_edge_above_frozen_holdout_ece():
    assert not nfl_calibration_safe(0.05)
    assert not nfl_calibration_safe(VALIDATION_ECE)
    assert nfl_calibration_safe(VALIDATION_ECE + 0.001)


def test_nfl_evidence_exposes_frozen_calibration_reference():
    game = NFLGame("g", 2027, "REG", "2027-09-20T12:00:00+00:00", "KC", "BAL", 0, 0)
    market = NormalizedMarket(title="BAL vs KC NFL game winner", **{**dict(venue=Venue.KALSHI, venue_market_id="nfl-meta", slug="nfl-meta", description="NFL game winner", category="NFL", event="e", outcomes={"YES":"YES","NO":"NO"}, resolution_rules="NFL game winner", resolution_time="2027-09-20T00:00:00Z", status="OPEN", yes_bid=.4, yes_ask=.5, no_bid=.4, no_ask=.5, best_bid_size=1, best_ask_size=1, executable_depth={}, recent_volume=None, recent_trade_count=None, last_trade_time=None, book_timestamp=None, data_timestamp="2027-01-01T00:00:00Z", source_url=None, mechanics=Mechanics()), "original_metadata":{"market":{"away_team":"BAL","home_team":"KC","marketType":"moneyline"}}})
    evidence = NFLEvidenceProvider(lambda: [game]).assess(market)
    assert evidence is not None and str(VALIDATION_ECE) in evidence.review_reference
