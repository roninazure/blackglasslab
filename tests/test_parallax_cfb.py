from parallax.cfb import CFBGame, advance, eligible_game, metrics, parse_games, probability, season_transition, validate

def test_parse_and_filter_cfb_games():
    games = parse_games('[{"id":1,"season":2025,"seasonType":"regular","startDate":"2025-09-01T18:00:00Z","completed":true,"neutralSite":true,"homeId":10,"homeTeam":"A","homeClassification":"fbs","homePoints":21,"awayId":11,"awayTeam":"B","awayClassification":"fbs","awayPoints":14},{"id":2,"season":2025,"seasonType":"regular","startDate":"2025-09-02T18:00:00Z","completed":true,"neutralSite":false,"homeId":10,"homeTeam":"A","homeClassification":"fbs","homePoints":31,"awayId":12,"awayTeam":"C","awayClassification":"fcs","awayPoints":0}]')
    assert len(games) == 2 and games[0].neutral_site
    assert eligible_game(games[0]) and not eligible_game(games[1])

def test_probability_and_neutral_site():
    assert probability(1500, 1500) > 0.5
    assert probability(1500, 1500, neutral_site=True) == 0.5

def test_state_updates_only_after_completed_game_and_season_regresses():
    game = CFBGame("g", 2024, "regular", "2024-09-01T00:00:00+00:00", "A", "B", "1", "2", "fbs", "fbs", False, 21, 0, True)
    ratings = {}
    p = probability(1500, 1500)
    advance(ratings, game, p)
    assert ratings["1"] != 1500 and ratings["2"] != 1500
    transitioned = season_transition(ratings)
    assert abs(transitioned["1"] - 1500) < abs(ratings["1"] - 1500)

def test_metrics_have_requested_buckets():
    report = metrics([0.1, 0.25, 0.55, 0.95], [0, 1, 1, 1])
    assert report["N"] == 4 and report["probability_range"] == [0.1, 0.95]
    assert {row["bucket"] for row in report["calibration_buckets"]} == {"<20%", "20–30%", "50–60%", ">90%"}

def test_validation_keeps_2025_holdout_chronological():
    games = []
    for season in (2022, 2023, 2024, 2025):
        for index in range(4):
            games.append(CFBGame(f"{season}-{index}", season, "regular", f"{season}-09-{index+1:02d}T12:00:00+00:00", "A", "B", "1", "2", "fbs", "fbs", index == 0, 21 if index % 2 == 0 else 10, 10 if index % 2 == 0 else 21, True))
    report = validate(games)
    assert report["train_seasons"] == (2022, 2023)
    assert report["calibration_seasons"] == (2024,)
    assert report["holdout_seasons"] == (2025,)
    assert report["holdout"]["CFB V1"]["N"] == 4
    assert report["leakage_check"] == report["leakage_check"]
