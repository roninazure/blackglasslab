import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from maker_spread_economics.live_engine import ReconciliationError, SafetyStop
from maker_spread_economics.polymarket_us import (
    PolymarketUSDiscoveryFailure,
    PolymarketUSPublicClient,
    PolymarketUSRateLimit,
)
from parallax.models import Action, Mechanics, NormalizedMarket, Side, Venue
from parallax.nfl import (
    VALIDATION_ECE,
    NFLEvidenceProvider,
    NFLGame,
    is_supported_market,
    map_market_to_game,
    nfl_calibration_safe,
    parse_games,
    validate,
)
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.sources import (
    KalshiPublicClient,
    KalshiPublicRateLimit,
    KalshiPublicSafetyStop,
)

nfl_live_scan_spec = importlib.util.spec_from_file_location(
    "nfl_live_scan", Path(__file__).parents[1] / "scripts" / "nfl_live_scan.py"
)
nfl_live_scan = importlib.util.module_from_spec(nfl_live_scan_spec)
assert nfl_live_scan_spec.loader is not None
nfl_live_scan_spec.loader.exec_module(nfl_live_scan)


class _JSONResponse:
    def __init__(self, payload, *, status=200):
        self.payload = json.dumps(payload).encode()
        self.status = status

    def __enter__(self):
        from io import BytesIO

        self._stream = BytesIO(self.payload)
        self.read = self._stream.read
        return self

    def __exit__(self, *_args):
        self._stream.close()


def test_kalshi_public_request_budget_blocks_underlying_call():
    calls = []

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        return _JSONResponse({"markets": []})

    client = KalshiPublicClient(
        opener=opener, minimum_interval_seconds=0, max_requests=1
    )
    assert client.markets_page(limit=1) == {"markets": []}
    with pytest.raises(KalshiPublicSafetyStop, match="budget exhausted"):
        client.book("KXNFLGAME-BLOCKED")
    assert len(calls) == 1


def test_kalshi_public_requests_are_spaced_per_client(monkeypatch):
    calls = []
    sleeps = []
    now = [0.0]

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        return _JSONResponse({"markets": []})

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr("parallax.sources.time.monotonic", lambda: now[0])
    monkeypatch.setattr("parallax.sources.time.sleep", sleep)
    client = KalshiPublicClient(
        opener=opener, minimum_interval_seconds=2, max_requests=2
    )
    client.markets_page(limit=1)
    client.markets_page(limit=1)

    assert len(calls) == 2
    assert sleeps == [2.0]


def test_kalshi_rate_limit_locks_client_without_second_provider_call():
    calls = []

    def opener(request, **_kwargs):
        calls.append(request.full_url)
        return _JSONResponse({}, status=429)

    client = KalshiPublicClient(
        opener=opener, minimum_interval_seconds=0, max_requests=5
    )
    with pytest.raises(KalshiPublicRateLimit):
        client.markets_page(limit=1)
    with pytest.raises(KalshiPublicRateLimit, match="locked out"):
        client.markets_page(limit=1)
    assert len(calls) == 1


def test_kalshi_nfl_discovery_cannot_exceed_hard_request_ceiling(monkeypatch):
    calls = []
    scheduled = [{
        "game_id": "g",
        "date": "2026-09-13",
        "start_time": "2026-09-13T20:25:00+00:00",
        "away_team": "ARI",
        "home_team": "LAC",
        "schedule_status": "SCHEDULED",
    }]

    class Client:
        def nfl_markets_page(self, *, limit, cursor):
            calls.append((limit, cursor))
            return {
                "markets": [
                    {
                        "ticker": "KXNFLGAME-26SEP13ARILAC-LAC",
                        "event_ticker": "KXNFLGAME-26SEP13ARILAC",
                    }
                ] * 100,
                "cursor": f"page-{len(calls)}",
            }

    monkeypatch.setattr(
        nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs)
    )
    _rows, coverage = nfl_live_scan._scope_kalshi(Client(), scheduled)

    assert len(calls) == nfl_live_scan.KALSHI_NFL_DISCOVERY_MAX_REQUESTS
    assert coverage["request_count"] == len(calls)
    assert coverage["request_ceiling"] == nfl_live_scan.KALSHI_NFL_DISCOVERY_MAX_REQUESTS
    assert coverage["state"] == "BOUNDED"


def test_kalshi_nfl_discovery_uses_one_series_request_and_schedule(monkeypatch):
    calls = []
    scheduled = [{
        "game_id": "g",
        "date": "2026-09-13",
        "start_time": "2026-09-13T20:25:00+00:00",
        "away_team": "ARI",
        "home_team": "LAC",
        "schedule_status": "SCHEDULED",
    }]

    class Client:
        def nfl_markets_page(self, *, limit, cursor):
            calls.append((limit, cursor))
            return {"markets": [
                {"ticker": "KXNFLGAME-26SEP13ARILAC-ARI", "event_ticker": "KXNFLGAME-26SEP13ARILAC"},
                {"ticker": "KXNFLGAME-26SEP13ARILAC-LAC", "event_ticker": "KXNFLGAME-26SEP13ARILAC"},
                {"ticker": "KXNFLGAME-26SEP14BUFNYJ-BUF", "event_ticker": "KXNFLGAME-26SEP14BUFNYJ"},
            ], "cursor": ""}

    monkeypatch.setattr(nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs))
    rows, coverage = nfl_live_scan._scope_kalshi(Client(), scheduled)

    assert calls == [(100, "")]
    assert [row["ticker"] for row in rows] == [
        "KXNFLGAME-26SEP13ARILAC-ARI",
        "KXNFLGAME-26SEP13ARILAC-LAC",
    ]
    assert all(row["home_team"] == "LAC" and row["away_team"] == "ARI" for row in rows)
    assert coverage["state"] == "COMPLETE"
    assert coverage["request_count"] == 1


def test_discovered_kalshi_game_contract_maps_from_schedule_enrichment(monkeypatch):
    scheduled = [{
        "game_id": "g",
        "date": "2026-09-13",
        "start_time": "2026-09-13T20:25:00+00:00",
        "away_team": "ARI",
        "home_team": "LAC",
        "schedule_status": "SCHEDULED",
    }]

    class Client:
        def nfl_markets_page(self, *, limit, cursor):
            return {"markets": [{
                "ticker": "KXNFLGAME-26SEP13ARILAC-LAC",
                "event_ticker": "KXNFLGAME-26SEP13ARILAC",
                "title": "Los Angeles C",
                "yes_sub_title": "LAC",
                "status": "open",
                "rules_primary": "Contract resolves from the official league result.",
            }], "cursor": ""}

    monkeypatch.setattr(nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs))
    rows, _coverage = nfl_live_scan._scope_kalshi(Client(), scheduled)
    market = normalize_kalshi(
        rows[0], {}, "2026-09-10T00:00:00+00:00", event=rows[0]["_discovery_event"]
    )
    game = NFLGame(
        "g", 2026, "REG", scheduled[0]["start_time"], "LAC", "ARI", None, None
    )

    assert is_supported_market(market)
    assert map_market_to_game(
        market, [game], now=datetime(2026, 9, 10, tzinfo=UTC)
    ).status == "MAPPED_GAME_WINNER"


def test_kalshi_mismatched_market_event_identity_fails_closed(monkeypatch):
    scheduled = [{
        "game_id": "g",
        "date": "2026-09-13",
        "start_time": "2026-09-13T20:25:00+00:00",
        "away_team": "ARI",
        "home_team": "LAC",
        "schedule_status": "SCHEDULED",
    }]
    source = {
        "ticker": "KXNFLGAME-26SEP14BUFNYJ-BUF",
        "event_ticker": "KXNFLGAME-26SEP13ARILAC",
    }

    class Client:
        def nfl_markets_page(self, *, limit, cursor):
            return {"markets": [source], "cursor": ""}

    monkeypatch.setattr(nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs))
    rows, coverage = nfl_live_scan._scope_kalshi(Client(), scheduled)

    assert rows == []
    assert coverage["state"] == "PARTIAL"
    assert coverage["failed_scopes"] == [{
        "scope": source["ticker"],
        "stage": "market_event_identity",
        "error": "TickerMismatch",
    }]
    assert "home_team" not in source and "scheduled_start" not in source


def test_existing_pmus_public_rate_limit_lockout_remains_intact():
    calls = []

    class RateLimitError(Exception):
        status_code = 429

    class Markets:
        def book(self, slug):
            calls.append(slug)
            raise RateLimitError("rate limited")

    client = PolymarketUSPublicClient(
        client=SimpleNamespace(markets=Markets()),
        minimum_interval_seconds=0,
        max_requests_per_minute=5,
    )
    with pytest.raises(PolymarketUSRateLimit):
        client.book("nfl-one")
    with pytest.raises(PolymarketUSRateLimit, match="locked out"):
        client.book("nfl-two")
    assert calls == ["nfl-one"]


def test_nfl_book_cache_reuses_one_book_for_both_scoring_sides():
    calls = []
    expected = {
        "nfl-one::YES": {"best_bid": 0.49, "best_ask": 0.51},
        "nfl-one::NO": {"best_bid": 0.49, "best_ask": 0.51},
    }

    class Client:
        def book(self, slug):
            calls.append(slug)
            return expected

    books = nfl_live_scan._PMUSBookCache(Client())

    assert books.get("nfl-one") is expected
    assert books.get("nfl-one") is expected
    assert calls == ["nfl-one"]


def test_nfl_book_cache_rate_limit_blocks_later_client_calls():
    calls = []

    class Client:
        def book(self, slug):
            calls.append(slug)
            raise PolymarketUSRateLimit("Cloudflare 1015")

    books = nfl_live_scan._PMUSBookCache(Client())

    with pytest.raises(PolymarketUSRateLimit):
        books.get("nfl-one")
    with pytest.raises(PolymarketUSRateLimit):
        books.get("nfl-two")
    assert calls == ["nfl-one"]


def test_nfl_book_cache_reuses_malformed_failure_and_fails_closed():
    calls = []

    class Markets:
        def book(self, slug):
            calls.append(slug)
            return {}

    client = PolymarketUSPublicClient(
        client=SimpleNamespace(markets=Markets()),
        minimum_interval_seconds=0,
    )
    books = nfl_live_scan._PMUSBookCache(client)

    with pytest.raises(ReconciliationError, match="market mismatch"):
        books.get("nfl-one")
    with pytest.raises(ReconciliationError, match="market mismatch"):
        books.get("nfl-one")
    assert calls == ["nfl-one"]


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


def test_nfl_upcoming_slate_count_is_schedule_driven_not_hardcoded():
    dt = __import__("datetime")
    now = dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
    games = [
        NFLGame(
            f"sun-{index}",
            2026,
            "REG",
            f"2026-09-27T{17 + index // 4:02d}:{(index % 4) * 10:02d}:00+00:00",
            f"H{index}",
            f"A{index}",
            None,
            None,
        )
        for index in range(14)
    ]
    games.append(
        NFLGame(
            "outside-window",
            2026,
            "REG",
            "2026-10-05T17:00:00+00:00",
            "HX",
            "AX",
            None,
            None,
        )
    )

    slate = nfl_live_scan._upcoming_slate(games, now)

    assert len(slate) == 14
    assert {row["game_id"] for row in slate} == {f"sun-{index}" for index in range(14)}
    assert {row["date"] for row in slate} == {"2026-09-27"}


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
    monkeypatch.setattr("parallax.nfl.utcnow", lambda: __import__("datetime").datetime(2026, 9, 21, tzinfo=__import__("datetime").UTC))
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
    monkeypatch.setattr("parallax.nfl.utcnow", lambda: __import__("datetime").datetime(2026, 9, 21, tzinfo=__import__("datetime").UTC))
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


def test_kalshi_nfl_family_supplies_winner_semantics_but_rejects_derivatives():
    raw = {
        "ticker": "KXNFLGAME-26SEP13ARILAC-LAC",
        "event_ticker": "KXNFLGAME-26SEP13ARILAC",
    }
    market = NormalizedMarket(
        venue=Venue.KALSHI,
        venue_market_id=raw["ticker"],
        slug=raw["ticker"],
        title="Los Angeles C",
        description="Los Angeles C",
        category="Uncategorized",
        event=raw["event_ticker"],
        outcomes={"YES": "Los Angeles C", "NO": "NO"},
        resolution_rules="Contract resolves from the official league result.",
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
    assert not is_supported_market(
        replace(
            market,
            title="Los Angeles C point spread",
            description="NFL spread",
            resolution_rules="Resolves against the point spread.",
        )
    )


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
    assert PolymarketUSPublicClient(
        client=client, minimum_interval_seconds=0
    ).markets_page(limit=100, offset=0) == []
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


def test_pmus_discovery_passes_supported_sports_category_filter():
    calls = []

    class Markets:
        def list(self, params):
            calls.append(params)
            return {"markets": []}

    client = SimpleNamespace(markets=Markets())
    assert PolymarketUSPublicClient(client=client).markets_page(
        limit=100, offset=0, categories=["sports"]
    ) == []
    assert calls == [
        {
            "active": True,
            "closed": False,
            "limit": 100,
            "offset": 0,
            "orderBy": ["volume"],
            "orderDirection": "desc",
            "categories": ["sports"],
        }
    ]


def test_pmus_discovery_passes_exact_schedule_slug_filter():
    calls = []

    class Markets:
        def list(self, params):
            calls.append(params)
            return {"markets": []}

    client = SimpleNamespace(markets=Markets())
    PolymarketUSPublicClient(client=client).markets_page(
        limit=2,
        offset=0,
        slugs=["nfl-ari-lac-2026-09-13", "aec-nfl-ari-lac-2026-09-13"],
    )
    assert calls[0]["slug"] == [
        "nfl-ari-lac-2026-09-13",
        "aec-nfl-ari-lac-2026-09-13",
    ]
    assert "categories" not in calls[0]


def test_pmus_discovery_rate_limit_is_no_retry_and_locks_out():
    calls = []

    class RateLimitError(Exception):
        status_code = 429

    class Markets:
        def list(self, params):
            calls.append(params)
            raise RateLimitError("rate limited")

    client = PolymarketUSPublicClient(
        client=SimpleNamespace(markets=Markets()), minimum_interval_seconds=0
    )
    with pytest.raises(PolymarketUSRateLimit):
        client.markets_page(limit=1, offset=0, slugs=["nfl-ari-lac-2026-09-13"])
    with pytest.raises(PolymarketUSRateLimit, match="locked out"):
        client.markets_page(limit=1, offset=0, slugs=["nfl-ari-lac-2026-09-13"])
    assert len(calls) == 1


def test_pmus_discovery_budget_exhaustion_blocks_provider_call():
    calls = []

    class Markets:
        def list(self, params):
            calls.append(params)
            return {"markets": []}

    client = PolymarketUSPublicClient(
        client=SimpleNamespace(markets=Markets()),
        minimum_interval_seconds=0,
        max_requests_per_minute=1,
    )
    client.markets_page(limit=1, offset=0, slugs=["nfl-ari-lac-2026-09-13"])
    with pytest.raises(PolymarketUSRateLimit, match="budget exhausted"):
        client.markets_page(limit=1, offset=0, slugs=["nfl-ari-lac-2026-09-13"])
    assert len(calls) == 1


def test_pmus_book_rate_limit_uses_public_client_lockout_semantics():
    class RateLimitError(Exception):
        status_code = 429
        body = "<html>Cloudflare Error 1015 " + ("x" * 5000) + "</html>"

    class Markets:
        def book(self, _slug):
            raise RateLimitError("You are being rate limited")

    with pytest.raises(SafetyStop) as caught:
        PolymarketUSPublicClient(client=SimpleNamespace(markets=Markets())).book(
            "nfl-cin-pit-2026-09-27"
        )

    assert type(caught.value) is PolymarketUSRateLimit
    assert str(caught.value) == (
        "public REST rate limited during book retrieval; "
        "further requests disabled for this client"
    )
    assert "<html>" not in str(caught.value)


def _run_pmus_scan(
    monkeypatch,
    tmp_path,
    *,
    rows,
    book,
    action=Action.PASS,
    discovery_error=None,
    scan_games=None,
    utcnow_values=None,
):
    constructed = []
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    game = NFLGame(
        "2026_04_CIN_PIT",
        2026,
        "REG",
        "2026-09-28T00:20:00+00:00",
        "PIT",
        "CIN",
        None,
        None,
    )

    class FakePMUS:
        def __init__(self, *args, **kwargs):
            self.closed = 0
            self.book_calls = []
            self.discovery_calls = []
            constructed.append(self)

        max_requests_per_minute = 20

        def markets_page(
            self,
            *,
            limit,
            offset,
            categories=None,
            slugs=None,
            retry_transport_errors=True,
        ):
            self.discovery_calls.append(
                (limit, offset, categories, slugs, retry_transport_errors)
            )
            if discovery_error is not None:
                raise discovery_error
            return rows

        def book(self, slug):
            self.book_calls.append(slug)
            return book(slug)

        def close(self):
            self.closed += 1

    market = SimpleNamespace(venue_market_id="825205", title="CIN at PIT")
    retail_examples = [SimpleNamespace(available=False, total_cost=None)] * 4

    def captured(_store, _market, side, _evidence, _now):
        return SimpleNamespace(
            id=f"play-{side.value}",
            venue="PMUS",
            market_id="825205",
            side=side,
            model_probability=0.5,
            executable_price=0.5,
            edge_points=0.0,
            fees_estimate=0.0,
            expected_value=0.0,
            executable_size=1.0,
            verdict=SimpleNamespace(failed_gates=()),
            suggested_action=action,
            retail_examples=retail_examples,
        )

    monkeypatch.setattr(nfl_live_scan, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(nfl_live_scan, "fetch_games", lambda: scan_games or [game])
    if utcnow_values is None:
        monkeypatch.setattr(nfl_live_scan, "utcnow", lambda: now)
    else:
        clock = iter(utcnow_values)
        monkeypatch.setattr(nfl_live_scan, "utcnow", lambda: next(clock))
    monkeypatch.setattr(nfl_live_scan, "TrackRecord", lambda _path: object())
    monkeypatch.setattr(
        nfl_live_scan,
        "default_inbox_store",
        lambda: SimpleNamespace(path=tmp_path / "inbox.sqlite"),
    )
    monkeypatch.setattr(nfl_live_scan, "AlertDeliveryStore", lambda _path: object())
    monkeypatch.setattr(nfl_live_scan, "AlertDispatcher", lambda _store: object())
    monkeypatch.setattr(nfl_live_scan, "KalshiPublicClient", lambda: object())
    monkeypatch.setattr(
        nfl_live_scan,
        "_scope_kalshi",
        lambda _client, _scheduled: ([], {"state": "COMPLETE", "failed_scopes": []}),
    )
    monkeypatch.setattr(nfl_live_scan, "normalize_pmus", lambda *_args, **_kwargs: market)
    monkeypatch.setattr(nfl_live_scan, "attach_fees", lambda value, *_args, **_kwargs: value)
    monkeypatch.setattr(nfl_live_scan, "is_supported_market", lambda _market: True)
    monkeypatch.setattr(
        nfl_live_scan,
        "map_market_to_game",
        lambda *_args, **_kwargs: SimpleNamespace(
            status="MAPPED_GAME_WINNER", reason="exact team/date match", game=game
        ),
    )
    monkeypatch.setattr(nfl_live_scan, "probability_for_game", lambda *_args: 0.5)
    monkeypatch.setattr(
        nfl_live_scan,
        "NFLEvidenceProvider",
        lambda _loader: SimpleNamespace(assess=lambda _market: object()),
    )
    monkeypatch.setattr(nfl_live_scan, "_capture_evaluated", captured)
    monkeypatch.setattr(nfl_live_scan, "_dispatch_buy_alert", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(nfl_live_scan, "reconcile_active_buy_alerts", lambda *_args, **_kwargs: {})

    return nfl_live_scan._scan(), constructed


def _pmus_nfl_row(market_id, slug):
    raw = {
        "id": market_id,
        "slug": slug,
        "question": "Cincinnati Bengals at Pittsburgh Steelers NFL game winner",
        "category": "sports",
        "marketType": "moneyline",
    }
    return {
        "id": market_id,
        "slug": slug,
        "question": raw["question"],
        "active": True,
        "closed": False,
        "raw": raw,
    }


def test_targeted_pmus_timeout_makes_exactly_one_provider_request(monkeypatch):
    calls = []

    class APITimeoutError(Exception):
        pass

    class Markets:
        def list(self, params):
            calls.append(params)
            raise APITimeoutError("read timeout")

    client = PolymarketUSPublicClient(
        client=SimpleNamespace(markets=Markets()), minimum_interval_seconds=0
    )
    scheduled = [{
        "game_id": "g",
        "date": "2026-09-13",
        "start_time": "2026-09-13T20:25:00+00:00",
        "away_team": "ARI",
        "home_team": "LAC",
        "schedule_status": "SCHEDULED",
    }]
    monkeypatch.setattr(nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs))

    with pytest.raises(PolymarketUSDiscoveryFailure) as caught:
        nfl_live_scan._scope_pmus(client, scheduled)

    assert caught.value.attempts == 1
    assert len(calls) == 1


def test_pmus_unexpected_returned_slug_is_partial_and_not_discovered(monkeypatch):
    class Client:
        max_requests_per_minute = 20

        def markets_page(self, **_kwargs):
            return [_pmus_nfl_row("wrong", "nfl-buf-nyj-2026-09-14")]

    scheduled = [{
        "game_id": "g",
        "date": "2026-09-13",
        "start_time": "2026-09-13T20:25:00+00:00",
        "away_team": "ARI",
        "home_team": "LAC",
        "schedule_status": "SCHEDULED",
    }]
    monkeypatch.setattr(nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs))

    rows, coverage = nfl_live_scan._scope_pmus(Client(), scheduled)

    assert rows == []
    assert coverage["state"] == "PARTIAL"
    assert coverage["unexpected_markets"] == 1


def test_pmus_oversized_slug_set_is_partial_without_request():
    class Client:
        max_requests_per_minute = 20

        def markets_page(self, **_kwargs):
            raise AssertionError("oversized slate must not call provider")

    scheduled = [
        {
            "game_id": f"g-{index}",
            "date": "2026-09-13",
            "start_time": "2026-09-13T20:25:00+00:00",
            "away_team": f"A{index}",
            "home_team": f"H{index}",
            "schedule_status": "SCHEDULED",
        }
        for index in range(51)
    ]

    rows, coverage = nfl_live_scan._scope_pmus(Client(), scheduled)

    assert rows == []
    assert coverage["state"] == "PARTIAL"
    assert coverage["request_count"] == 0


def test_pmus_malformed_slate_raises_before_provider_request():
    calls = []

    class Client:
        max_requests_per_minute = 20

        def markets_page(self, **_kwargs):
            calls.append(1)

    with pytest.raises(KeyError):
        nfl_live_scan._scope_pmus(Client(), [{"date": "2026-09-13"}])
    assert calls == []


def test_pmus_empty_slate_is_complete_without_request():
    class Client:
        max_requests_per_minute = 20

        def markets_page(self, **_kwargs):
            raise AssertionError("empty slate must not call provider")

    rows, coverage = nfl_live_scan._scope_pmus(Client(), [])

    assert rows == []
    assert coverage["state"] == "COMPLETE"
    assert coverage["request_count"] == 0


def test_pmus_la_team_code_constructs_plain_and_aec_lar_slugs():
    scheduled = [{
        "game_id": "g",
        "date": "2026-09-21",
        "start_time": "2026-09-22T00:15:00+00:00",
        "away_team": "NYG",
        "home_team": "LA",
        "schedule_status": "SCHEDULED",
    }]

    assert nfl_live_scan._pmus_slugs(scheduled) == [
        "nfl-nyg-lar-2026-09-21",
        "aec-nfl-nyg-lar-2026-09-21",
    ]


def test_nfl_scan_reuses_one_pmus_client_for_discovery_and_multiple_books(monkeypatch, tmp_path):
    rows = [
        _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27"),
        _pmus_nfl_row("825205-aec", "aec-nfl-cin-pit-2026-09-27"),
    ]
    result, clients = _run_pmus_scan(
        monkeypatch, tmp_path, rows=rows, book=lambda slug: {"slug": slug}
    )

    assert len(clients) == 1
    assert clients[0].discovery_calls == [(
        2,
        0,
        None,
        ["nfl-cin-pit-2026-09-27", "aec-nfl-cin-pit-2026-09-27"],
        False,
    )]
    assert clients[0].book_calls == [row["slug"] for row in rows]
    assert clients[0].closed == 1
    assert result["read_only"] is True and result["orders"] == 0


def test_nfl_scan_reuses_duplicate_market_book_for_yes_and_no(monkeypatch, tmp_path):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")
    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row, dict(row)],
        book=lambda slug: {"slug": slug},
    )

    assert clients[0].book_calls == [row["slug"]]
    assert len([item for item in result["summary"]["rows"] if "verdict" in item]) == 4


def test_nfl_scan_rate_limit_stops_all_later_pmus_book_calls(monkeypatch, tmp_path):
    rows = [
        _pmus_nfl_row("plain", "nfl-cin-pit-2026-09-27"),
        _pmus_nfl_row("aec", "aec-nfl-cin-pit-2026-09-27"),
    ]

    def rate_limited(_slug):
        raise PolymarketUSRateLimit("Cloudflare 1015")

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=rows,
        book=rate_limited,
    )

    assert clients[0].book_calls == [rows[0]["slug"]]
    failures = [
        item for item in result["summary"]["rows"] if "scoring_error" in item
    ]
    assert len(failures) == 2
    assert {item["scoring_error"] for item in failures} == {"RATE_LIMITED"}


def test_nfl_pmus_rate_limit_is_data_unavailable_with_bounded_diagnostic(monkeypatch, tmp_path):
    html = "<html>Cloudflare Error 1015 You are being rate limited " + ("x" * 5000) + "</html>"

    def rate_limited(_slug):
        raise SafetyStop(html)

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")],
        book=rate_limited,
        action=Action.BUY,
    )

    diagnostic = next(
        row for row in result["summary"]["rows"] if "scoring_error" in row
    )
    assert diagnostic["scoring_error"] == "RATE_LIMITED"
    assert diagnostic["scoring_error_reason"] == (
        "Polymarket US public API rate limited (Cloudflare 1015)"
    )
    assert "<html>" not in json.dumps(diagnostic)
    assert result["slate"]["status_counts"] == {"DATA_UNAVAILABLE": 1}
    assert not any("verdict" in row for row in result["summary"]["rows"])
    assert result["read_only"] is True and result["orders"] == 0
    assert len(clients) == 1 and clients[0].closed == 1


def test_incomplete_pmus_discovery_keeps_market_data_incomplete(monkeypatch, tmp_path):
    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[],
        book=lambda _slug: {},
        discovery_error=PolymarketUSRateLimit(
            "public REST safety budget exhausted during market discovery"
        ),
    )

    assert result["venues"]["PMUS"]["coverage"]["state"] == "PARTIAL"
    assert result["slate"]["market_data_complete"] is False
    assert result["slate"]["status_counts"] == {"NO_MARKET": 1}
    assert len(clients) == 1 and clients[0].closed == 1


def test_scan_reuses_discovery_slate_across_eastern_date_boundary(monkeypatch, tmp_path):
    discovery_at = datetime(2026, 9, 28, 3, 59, tzinfo=UTC)
    lifecycle_at = datetime(2026, 9, 28, 4, 1, tzinfo=UTC)
    games = [
        NFLGame(
            "visible-at-discovery",
            2026,
            "REG",
            "2026-10-04T17:00:00+00:00",
            "PIT",
            "CIN",
            None,
            None,
        ),
        NFLGame(
            "enters-after-midnight",
            2026,
            "REG",
            "2026-10-05T17:00:00+00:00",
            "LAC",
            "ARI",
            None,
            None,
        ),
    ]

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[],
        book=lambda _slug: {},
        scan_games=games,
        utcnow_values=[discovery_at, lifecycle_at],
    )

    assert result["slate"]["expected_games"] == 1
    assert [row["game_id"] for row in result["slate"]["dates"][0]["games"]] == [
        "visible-at-discovery"
    ]
    assert result["slate"]["market_data_complete"] is True
    assert clients[0].discovery_calls[0][3] == [
        "nfl-cin-pit-2026-10-04",
        "aec-nfl-cin-pit-2026-10-04",
    ]


@pytest.mark.parametrize("action", [Action.BUY, Action.WATCH, Action.PASS])
def test_successful_nfl_pmus_scoring_preserves_action(monkeypatch, tmp_path, action):
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")],
        book=lambda slug: {"slug": slug},
        action=action,
    )

    verdicts = [row["verdict"] for row in result["summary"]["rows"] if "verdict" in row]
    assert verdicts == [action.value, action.value]
    assert result["slate"]["status_counts"] == {action.value: 1}
    assert result["read_only"] is True and result["orders"] == 0



def test_nfl_scored_buy_dispatches_immediate_alert(monkeypatch):
    sent = []

    def fake_dispatch(dispatcher, scored_play, scored_market, **kwargs):
        sent.append((dispatcher, scored_play, scored_market, kwargs))
        return {
            "status": "SENT",
            "deduplicated": False,
            "http_status": 200,
            "error_code": None,
        }

    monkeypatch.setattr(nfl_live_scan, "dispatch_scored_buy", fake_dispatch)
    game = SimpleNamespace(
        away_team="BAL",
        home_team="KC",
        kickoff="2026-09-28T00:20:00+00:00",
    )
    mapping = SimpleNamespace(game=game)
    scored_play = SimpleNamespace(suggested_action=Action.BUY)
    detected_at = __import__("datetime").datetime(
        2026, 9, 24, 22, 30, tzinfo=__import__("datetime").UTC
    )

    result = nfl_live_scan._dispatch_buy_alert(
        object(), scored_play, object(), mapping, detected_at
    )

    assert result["status"] == "SENT"
    assert len(sent) == 1
    assert sent[0][3]["sport"] == "NFL"
    assert sent[0][3]["matchup"] == "BAL at KC"
    assert sent[0][3]["detected_at"] == detected_at.isoformat()
    assert sent[0][3]["game_start"] == "2026-09-28T00:20:00+00:00"


@pytest.mark.parametrize("action", [__import__("parallax.models", fromlist=["Action"]).Action.WATCH, __import__("parallax.models", fromlist=["Action"]).Action.PASS])
def test_nfl_watch_and_pass_do_not_dispatch_buy_alert(monkeypatch, action):
    monkeypatch.setattr(
        nfl_live_scan,
        "dispatch_scored_buy",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("WATCH/PASS must stay silent")
        ),
    )
    play = __import__("types").SimpleNamespace(suggested_action=action)
    mapping = __import__("types").SimpleNamespace(game=None)

    assert (
        nfl_live_scan._dispatch_buy_alert(
            object(),
            play,
            __import__("types").SimpleNamespace(title="test"),
            mapping,
            __import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        is None
    )
