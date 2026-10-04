import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from maker_spread_economics.live_engine import ReconciliationError, SafetyStop
from maker_spread_economics.polymarket_us import (
    PolymarketUSDiscoveryFailure,
    PolymarketUSPublicClient,
    PolymarketUSRateLimit,
)
from parallax.engine import MAX_AGE_SECONDS
from parallax.fees import REVIEW_EXPIRES, REVIEWED_AT, attach_fees
from parallax.models import Action, Mechanics, NormalizedMarket, Side, Venue, timestamp
from parallax.nfl import (
    VALIDATION_ECE,
    NFLEvidenceProvider,
    NFLGame,
    economic_team_for_side,
    is_supported_market,
    market_support_reason,
    map_market_to_game,
    nfl_calibration_safe,
    parse_games,
    validate,
)
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.pmus_acquisition import PMUSAcquisition, PMUSAcquisitionUnavailable
from parallax.pmus_market_data import PMUSMarketDataUnavailable, PMUSStreamBook
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


def test_nfl_buy_freshness_window_remains_sixty_seconds():
    assert MAX_AGE_SECONDS == 60


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
    assert all(
        row["_discovery_event"]
        == {
            "ticker": "KXNFLGAME-26SEP13ARILAC",
            "event_ticker": "KXNFLGAME-26SEP13ARILAC",
            "series_ticker": "KXNFLGAME",
            "title": "ARI vs LAC NFL game",
            "sport": "NFL",
            "away_team": "ARI",
            "home_team": "LAC",
            "scheduled_start": "2026-09-13T20:25:00+00:00",
        }
        for row in rows
    )
    assert all(
        row["_discovery_series"]
        == {
            "ticker": "KXNFLGAME",
            "sport": "NFL",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
        }
        for row in rows
    )
    assert coverage["state"] == "COMPLETE"
    assert coverage["request_count"] == 1


def test_discovered_kalshi_nfl_fee_policy_is_verified_and_bounded(monkeypatch):
    reviewed_at = timestamp(REVIEWED_AT)
    expires_at = timestamp(REVIEW_EXPIRES)
    assert reviewed_at is not None and expires_at is not None
    now = reviewed_at + timedelta(minutes=1)
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
                "title": "Los Angeles C wins",
                "yes_sub_title": "LAC",
                "status": "open",
                "rules_primary": "Contract resolves from the official league result.",
            }], "cursor": ""}

    monkeypatch.setattr(
        nfl_live_scan, "_scoped_call", lambda function, **kwargs: function(**kwargs)
    )
    rows, _coverage = nfl_live_scan._scope_kalshi(Client(), scheduled)
    row = rows[0]
    market = normalize_kalshi(
        row, {}, now.isoformat(), event=row["_discovery_event"]
    )

    verified = attach_fees(
        market,
        now,
        event=row["_discovery_event"],
        series=row["_discovery_series"],
    )

    assert verified.mechanics.fee_rate == 0.07
    assert verified.mechanics.fee_status == "VERIFIED_UPPER_BOUND"
    assert timestamp(verified.mechanics.fee_valid_until) == now + timedelta(seconds=60)
    assert verified.original_metadata["fee_provenance"]["effective_multiplier"] == 1
    assert verified.original_metadata["fee_provenance"]["verified_series"] == "KXNFLGAME"

    event = row["_discovery_event"]
    series = row["_discovery_series"]
    assert attach_fees(
        market, now, event={**event, "event_ticker": "WRONG"}, series=series
    ).mechanics.fee_rate is None
    assert attach_fees(
        market, now, event=event, series={**series, "ticker": "WRONG"}
    ).mechanics.fee_rate is None
    assert attach_fees(
        market,
        now,
        event={**event, "fee_type_override": "unsupported"},
        series=series,
    ).mechanics.fee_rate is None
    assert attach_fees(
        market, now + timedelta(seconds=60), event=event, series=series
    ).mechanics.fee_rate is None
    assert attach_fees(
        replace(market, data_timestamp=expires_at.isoformat()),
        expires_at,
        event=event,
        series=series,
    ).mechanics.fee_rate is None


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


def test_nfl_book_cache_reuses_one_book_for_both_scoring_sides(tmp_path):
    calls = []
    expected = {
        "nfl-one::YES": {"best_bid": 0.49, "best_ask": 0.51},
        "nfl-one::NO": {"best_bid": 0.49, "best_ask": 0.51},
    }

    class Client:
        def book(self, slug):
            calls.append(slug)
            return expected

    client = Client()
    books = PMUSAcquisition("NFL", state_path=tmp_path / "pmus.sqlite")

    assert books.book(
        "nfl-one", lambda: client.book("nfl-one"), fair_probability=0.7
    ).book == expected
    assert books.book(
        "nfl-one", lambda: client.book("nfl-one"), fair_probability=0.7
    ).book == expected
    assert calls == ["nfl-one"]


def test_nfl_book_cache_rate_limit_blocks_later_client_calls(tmp_path):
    calls = []

    class Client:
        def book(self, slug):
            calls.append(slug)
            raise PolymarketUSRateLimit("Cloudflare 1015")

    client = Client()
    books = PMUSAcquisition("NFL", state_path=tmp_path / "pmus.sqlite")

    with pytest.raises(PolymarketUSRateLimit):
        books.book(
            "nfl-one", lambda: client.book("nfl-one"), fair_probability=0.7
        )
    with pytest.raises(PolymarketUSRateLimit):
        books.book(
            "nfl-two", lambda: client.book("nfl-two"), fair_probability=0.7
        )
    assert calls == ["nfl-one"]


def test_nfl_book_cache_reuses_malformed_failure_and_fails_closed(tmp_path):
    calls = []

    class Markets:
        def book(self, slug):
            calls.append(slug)
            return {}

    client = PolymarketUSPublicClient(
        client=SimpleNamespace(markets=Markets()),
        minimum_interval_seconds=0,
    )
    books = PMUSAcquisition("NFL", state_path=tmp_path / "pmus.sqlite")

    with pytest.raises(ReconciliationError, match="market mismatch"):
        books.book(
            "nfl-one", lambda: client.book("nfl-one"), fair_probability=0.7
        )
    with pytest.raises(PMUSAcquisitionUnavailable, match="cached PMUS book failure"):
        books.book(
            "nfl-one", lambda: client.book("nfl-one"), fair_probability=0.7
        )
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


def test_kalshi_nfl_disclaimer_property_does_not_trigger_prop_rejection():
    raw = {
        "ticker": "KXNFLGAME-26SEP13ARILAC-LAC",
        "event_ticker": "KXNFLGAME-26SEP13ARILAC",
        "title": "Los Angeles C wins",
        "yes_sub_title": "LAC",
        "no_sub_title": "LAC",
        "status": "open",
        "market_type": "binary",
        "expected_expiration_time": "2026-09-14T02:25:00Z",
        "rules_primary": (
            "If Los Angeles C wins the Arizona vs Los Angeles C professional "
            "football game, then the market resolves to Yes."
        ),
        "rules_secondary": (
            "All team names, logos, and other marks are property of their "
            "respective owners."
        ),
        "away_team": "ARI",
        "home_team": "LAC",
        "scheduled_start": "2026-09-13T20:25:00+00:00",
    }
    market = normalize_kalshi(raw, {}, "2026-09-10T00:00:00+00:00")
    game = NFLGame(
        "g", 2026, "REG", raw["scheduled_start"], "LAC", "ARI", None, None
    )

    assert market_support_reason(market) == "SUPPORTED"
    assert map_market_to_game(
        market, [game], now=datetime(2026, 9, 10, tzinfo=UTC)
    ).status == "MAPPED_GAME_WINNER"


def test_nfl_scan_reports_redacted_rejections_and_scores_property_disclaimer_market(
    monkeypatch, tmp_path
):
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    game = NFLGame(
        "g", 2026, "REG", "2026-09-13T20:25:00+00:00", "LAC", "ARI", None, None
    )
    event_ticker = "KXNFLGAME-26SEP13ARILAC"

    def raw_market(ticker, title, rules):
        return {
            "ticker": ticker,
            "event_ticker": event_ticker,
            "title": title,
            "yes_sub_title": "LAC",
            "no_sub_title": "LAC",
            "status": "open",
            "market_type": "binary",
            "expected_expiration_time": "2026-09-14T02:25:00Z",
            "price_level_structure": "linear_cent",
            "rules_primary": rules,
            "away_team": "ARI",
            "home_team": "LAC",
            "scheduled_start": game.kickoff,
        }

    supported = raw_market(
        f"{event_ticker}-LAC",
        "Los Angeles C wins",
        (
            "If Los Angeles C wins the Arizona vs Los Angeles C professional "
            "football game, then the market resolves to Yes. All team names and "
            "logos are property of their respective owners."
        ),
    )
    derivative = raw_market(
        f"{event_ticker}-LACSPREAD",
        "Los Angeles C point spread",
        "This contract resolves against the point spread.",
    )
    event = {
        "ticker": event_ticker,
        "title": "ARI vs LAC NFL game",
        "sport": "NFL",
        "away_team": "ARI",
        "home_team": "LAC",
        "scheduled_start": game.kickoff,
    }
    series = {"ticker": "KXNFLGAME", "sport": "NFL"}
    rows = [
        {**supported, "_discovery_event": event, "_discovery_series": series},
        {**derivative, "_discovery_event": event, "_discovery_series": series},
    ]

    class FakePMUS:
        def close(self):
            pass

    class FakeKalshi:
        def __init__(self):
            self.book_calls = []

        def book(self, ticker):
            self.book_calls.append(ticker)
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.45", "100"]],
                    "no_dollars": [["0.50", "100"]],
                }
            }

    kalshi = FakeKalshi()
    captured = []
    retail_examples = [SimpleNamespace(available=False, total_cost=None)] * 4

    def capture(_store, market, side, _evidence, _decision_at):
        captured.append((market.venue_market_id, side))
        return SimpleNamespace(
            id=f"play-{side.value}",
            venue=Venue.KALSHI,
            market_id=market.venue_market_id,
            side=side,
            model_probability=0.5,
            executable_price=0.5,
            edge_points=0.0,
            fees_estimate=0.0,
            expected_value=0.0,
            executable_size=1.0,
            verdict=SimpleNamespace(failed_gates=()),
            suggested_action=Action.WATCH,
            retail_examples=retail_examples,
        )

    monkeypatch.setattr(nfl_live_scan, "fetch_games", lambda: [game])
    monkeypatch.setattr(nfl_live_scan, "utcnow", lambda: now)
    monkeypatch.setattr("parallax.nfl.utcnow", lambda: now)
    monkeypatch.setattr(nfl_live_scan, "TrackRecord", lambda _path: object())
    monkeypatch.setattr(
        nfl_live_scan,
        "default_inbox_store",
        lambda: SimpleNamespace(path=tmp_path / "inbox.sqlite"),
    )
    monkeypatch.setattr(nfl_live_scan, "AlertDeliveryStore", lambda _path: object())
    monkeypatch.setattr(nfl_live_scan, "AlertDispatcher", lambda _store: object())
    monkeypatch.setattr(nfl_live_scan, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(
        nfl_live_scan,
        "_scope_pmus",
        lambda *_args: ([], {"state": "COMPLETE", "failed_scopes": []}),
    )
    monkeypatch.setattr(nfl_live_scan, "KalshiPublicClient", lambda: kalshi)
    monkeypatch.setattr(
        nfl_live_scan,
        "_scope_kalshi",
        lambda *_args: (rows, {"state": "COMPLETE", "failed_scopes": []}),
    )
    monkeypatch.setattr(nfl_live_scan, "attach_fees", lambda market, *_args, **_kwargs: market)
    monkeypatch.setattr(nfl_live_scan, "probability_for_game", lambda *_args: 0.5)
    monkeypatch.setattr(
        nfl_live_scan,
        "NFLEvidenceProvider",
        lambda _loader: SimpleNamespace(
            assess=lambda _market: SimpleNamespace(fair_probability=0.5)
        ),
    )
    monkeypatch.setattr(nfl_live_scan, "_capture_evaluated", capture)
    monkeypatch.setattr(nfl_live_scan, "_dispatch_buy_alert", lambda *_args: None)
    monkeypatch.setattr(
        nfl_live_scan, "reconcile_active_buy_alerts", lambda *_args, **_kwargs: {}
    )
    acquisition = SimpleNamespace(diagnostics=lambda: {})

    result = nfl_live_scan._scan(pmus_acquisition=acquisition)

    assert kalshi.book_calls == [supported["ticker"]]
    assert captured == [(supported["ticker"], Side.YES), (supported["ticker"], Side.NO)]
    assert result["summary"]["rejection_counts"] == {"REJECTED_DERIVATIVE": 1}
    assert result["summary"]["status_counts"]["MAPPED_GAME_WINNER"] == 1
    assert result["summary"]["status_counts"]["SCORED_WATCH"] == 2


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
    assert not is_supported_market(
        replace(
            market,
            title="NFL player props",
            description="Touchdowns and other player props",
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
    action_by_side=None,
    dispatched=None,
    discovery_error=None,
    kalshi_coverage_state="COMPLETE",
    scan_games=None,
    utcnow_values=None,
    scan_clock=None,
    action_by_market_side=None,
    kalshi_rows=None,
    kalshi_book=None,
    evidence_for_market=None,
    mapping_for_market=None,
    pmus_acquisition=None,
    stream_factory=None,
    direct_scope=False,
    fast_publisher=None,
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

    class FakeKalshi:
        def book(self, ticker):
            if kalshi_book is None:
                return {}
            return kalshi_book(ticker)

    def normalized_market(raw, venue):
        return SimpleNamespace(
            venue_market_id=raw["id"] if venue is Venue.POLYMARKET else raw["ticker"],
            title="CIN at PIT",
            venue=venue,
            status="OPEN",
        )

    retail_examples = [SimpleNamespace(available=False, total_cost=None)] * 4

    def captured(_store, _market, side, _evidence, _now):
        selected_action = (
            action_by_market_side(_market, side)
            if action_by_market_side
            else action_by_side(side)
            if action_by_side
            else action
        )
        return SimpleNamespace(
            id=f"play-{_market.venue.value}-{_market.venue_market_id}-{side.value}",
            venue=_market.venue,
            market_id=_market.venue_market_id,
            side=side,
            side_description="Cincinnati Bengals" if side == Side.YES else "Pittsburgh Steelers",
            model_probability=0.5,
            executable_price=0.5,
            edge_points=10.0,
            fees_estimate=0.0,
            expected_value=1.0,
            expected_return=0.1,
            executable_size=1.0,
            verdict=SimpleNamespace(failed_gates=()),
            suggested_action=selected_action,
            retail_examples=retail_examples,
            created_at=_now.isoformat(),
            updated_at=_now.isoformat(),
            expires_at=(_now + timedelta(seconds=60)).isoformat(),
            data_freshness="FRESH",
            status="CURRENT",
            demo=False,
            evidence=object(),
        )

    monkeypatch.setattr(nfl_live_scan, "PolymarketUSPublicClient", FakePMUS)
    if direct_scope:
        monkeypatch.setattr(
            nfl_live_scan,
            "_scope_pmus",
            lambda _client, _scheduled, _acquisition: (
                rows,
                {"state": "COMPLETE", "request_count": 0},
            ),
        )
    monkeypatch.setattr(nfl_live_scan, "fetch_games", lambda: scan_games or [game])
    if scan_clock is not None:
        monkeypatch.setattr(nfl_live_scan, "utcnow", lambda: scan_clock[0])
    elif utcnow_values is None:
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
    monkeypatch.setattr(nfl_live_scan, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(
        nfl_live_scan,
        "_scope_kalshi",
        lambda _client, _scheduled: (
            kalshi_rows or [],
            {"state": kalshi_coverage_state, "failed_scopes": []},
        ),
    )
    monkeypatch.setattr(
        nfl_live_scan,
        "normalize_pmus",
        lambda raw, *_args, **_kwargs: normalized_market(raw, Venue.POLYMARKET),
    )
    monkeypatch.setattr(
        nfl_live_scan,
        "normalize_kalshi",
        lambda raw, *_args, **_kwargs: normalized_market(raw, Venue.KALSHI),
    )
    monkeypatch.setattr(nfl_live_scan, "attach_fees", lambda value, *_args, **_kwargs: value)
    monkeypatch.setattr(
        nfl_live_scan, "market_support_reason", lambda _market: "SUPPORTED"
    )
    monkeypatch.setattr(
        nfl_live_scan,
        "map_market_to_game",
        lambda market, *_args, **_kwargs: (
            mapping_for_market(market)
            if mapping_for_market
            else SimpleNamespace(
                status="MAPPED_GAME_WINNER",
                reason="exact team/date match",
                game=next(
                    candidate
                    for candidate in (scan_games or [game])
                    if candidate.game_id == (
                        "game-b"
                        if market.venue_market_id == "b-pmus"
                        else (scan_games or [game])[0].game_id
                    )
                ),
                selected_team=(
                    "LAC" if market.venue_market_id == "b-pmus" else "CIN"
                ),
            )
        ),
    )
    monkeypatch.setattr(nfl_live_scan, "probability_for_game", lambda *_args: 0.5)
    monkeypatch.setattr(
        nfl_live_scan,
        "NFLEvidenceProvider",
        lambda _loader: SimpleNamespace(
            assess=lambda scored_market: (
                evidence_for_market(scored_market)
                if evidence_for_market
                else SimpleNamespace(fair_probability=0.5)
            )
        ),
    )
    monkeypatch.setattr(
        nfl_live_scan,
        "_capture_evaluated",
        lambda store, scored_market, side, evidence, decision_at: SimpleNamespace(
            **{
                **vars(captured(store, scored_market, side, evidence, decision_at)),
                "market_id": scored_market.venue_market_id,
            }
        ),
    )
    if dispatched is None:
        monkeypatch.setattr(
            nfl_live_scan, "_dispatch_buy_alert", lambda *_args, **_kwargs: None
        )
    else:
        def capture_dispatch(_dispatcher, play, market, mapping, detected_at, **kwargs):
            dispatched.append((play, market, mapping, detected_at, kwargs))
            return {
                "status": "SENT",
                "deduplicated": False,
                "http_status": 200,
                "error_code": None,
            }

        monkeypatch.setattr(nfl_live_scan, "_dispatch_buy_alert", capture_dispatch)
    monkeypatch.setattr(nfl_live_scan, "reconcile_active_buy_alerts", lambda *_args, **_kwargs: {})

    acquisition_now = [now]

    def advance(seconds):
        acquisition_now[0] += timedelta(seconds=seconds)

    acquisition = pmus_acquisition or PMUSAcquisition(
        "NFL",
        state_path=tmp_path / "pmus-acquisition.sqlite",
        clock=lambda: acquisition_now[0],
        sleeper=advance,
    )
    if stream_factory is None:
        def stream_factory(_slugs):
            raise PMUSMarketDataUnavailable("offline test stream unavailable")

    return nfl_live_scan._scan(
        pmus_acquisition=acquisition,
        pmus_market_data_factory=stream_factory,
        fast_publisher=fast_publisher,
    ), constructed


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


def _kalshi_nfl_row(market_id="kalshi-cin"):
    return {
        "ticker": market_id,
        "_discovery_event": {},
        "_discovery_series": {},
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


class _FakePMUSStream:
    def __init__(
        self,
        slugs,
        books=None,
        *,
        fail_books=False,
        missing_slugs=(),
        wait_ready=True,
        failed=False,
        book_error=None,
    ):
        self.slugs = list(slugs)
        self.books = books or {}
        self.fail_books = fail_books
        self.missing_slugs = set(missing_slugs)
        self.wait_ready_result = wait_ready
        self.failed = failed
        self.book_error = book_error
        self.started = False
        self.stopped = False
        self.book_calls = []
        self.error = None

    def start(self):
        self.started = True

    def wait_ready(self, _timeout):
        return self.wait_ready_result

    def book(self, slug):
        self.book_calls.append(slug)
        if self.book_error is not None:
            raise self.book_error
        if self.fail_books or slug in self.missing_slugs:
            raise PMUSMarketDataUnavailable("stream unavailable")
        return PMUSStreamBook(
            self.books.get(slug, {"slug": slug}),
            "2026-09-27T12:00:00+00:00",
            10.0,
        )

    def diagnostics(self):
        fresh_books = len(self.slugs) - len(self.missing_slugs)
        return {
            "state": "FAILED" if self.failed else "HEALTHY",
            "markets_requested": len(self.slugs),
            "fresh_books": fresh_books,
            "readiness": (
                "FULL"
                if fresh_books == len(self.slugs)
                else "PARTIAL"
                if fresh_books
                else "NONE"
            ),
            "rest_polling": False,
        }

    def stop(self):
        self.stopped = True
        return True


def test_nfl_uses_fresh_stream_without_rest_book_call(monkeypatch, tmp_path):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")
    streams = []

    def stream_factory(slugs):
        stream = _FakePMUSStream(slugs)
        streams.append(stream)
        return stream

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row],
        book=lambda _slug: (_ for _ in ()).throw(
            AssertionError("REST book must not be called for a fresh stream book")
        ),
        stream_factory=stream_factory,
    )

    assert streams[0].slugs == [row["slug"]]
    assert streams[0].started and streams[0].stopped
    assert streams[0].book_calls == [row["slug"]]
    assert clients[0].book_calls == []
    assert result["pmus_acquisition"]["book_requests"] == 0
    assert result["summary"]["status_counts"]["BOOK_STREAMED"] == 1
    assert result["pmus_market_data"]["shutdown_clean"] is True


def test_nfl_stream_failure_falls_back_only_through_acquisition(monkeypatch, tmp_path):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")
    streams = []

    def stream_factory(slugs):
        stream = _FakePMUSStream(slugs, fail_books=True)
        streams.append(stream)
        return stream

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row],
        book=lambda slug: {"slug": slug},
        stream_factory=stream_factory,
    )

    assert streams[0].book_calls == [row["slug"]]
    assert clients[0].book_calls == [row["slug"]]
    assert result["pmus_acquisition"]["book_requests"] == 1
    assert result["pmus_acquisition"]["candidates_considered"] == 1


def test_nfl_partial_stream_keeps_sixteen_books_and_falls_back_only_for_missing(
    monkeypatch, tmp_path
):
    rows = [_pmus_nfl_row(str(index), f"nfl-partial-{index}") for index in range(17)]
    missing_slug = rows[-1]["slug"]
    streams = []

    def stream_factory(slugs):
        stream = _FakePMUSStream(
            slugs,
            missing_slugs={missing_slug},
            wait_ready=False,
        )
        streams.append(stream)
        return stream

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=rows,
        book=lambda slug: {"slug": slug},
        stream_factory=stream_factory,
        direct_scope=True,
    )

    assert streams[0].stopped is True
    assert clients[0].book_calls == [missing_slug]
    assert result["summary"]["status_counts"]["BOOK_STREAMED"] == 16
    assert result["pmus_acquisition"]["book_requests"] == 1
    assert result["pmus_market_data"]["startup_readiness"] == "PARTIAL"
    assert result["pmus_market_data"]["readiness"] == "PARTIAL"


def test_nfl_terminal_startup_stream_failure_uses_governed_fallback(
    monkeypatch, tmp_path
):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row],
        book=lambda slug: {"slug": slug},
        stream_factory=lambda slugs: _FakePMUSStream(
            slugs,
            fail_books=True,
            wait_ready=False,
            failed=True,
        ),
    )

    assert clients[0].book_calls == [row["slug"]]
    assert result["pmus_acquisition"]["book_requests"] == 1
    assert result["pmus_market_data"]["state"] == "UNAVAILABLE"
    assert result["pmus_market_data"]["startup_readiness"] == "FAILED"


def test_nfl_unexpected_stream_startup_exception_fails_closed_without_rest(
    monkeypatch, tmp_path
):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")

    def broken_factory(_slugs):
        raise RuntimeError("stream implementation bug")

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row],
        book=lambda _slug: (_ for _ in ()).throw(
            AssertionError("unexpected startup errors must not call REST")
        ),
        stream_factory=broken_factory,
    )

    assert clients[0].book_calls == []
    assert result["pmus_acquisition"]["book_requests"] == 0
    assert result["pmus_market_data"]["state"] == "FAILED"
    failure = next(
        item for item in result["summary"]["rows"] if "scoring_error" in item
    )
    assert failure["scoring_error"] == "RuntimeError"
    assert not any("verdict" in item for item in result["summary"]["rows"])


@pytest.mark.parametrize("unexpected", [RuntimeError("bug"), TypeError("bug")])
def test_nfl_unexpected_stream_exception_fails_closed_without_rest(
    monkeypatch, tmp_path, unexpected
):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row],
        book=lambda _slug: (_ for _ in ()).throw(
            AssertionError("unexpected stream errors must not call REST")
        ),
        stream_factory=lambda slugs: _FakePMUSStream(
            slugs, book_error=unexpected
        ),
    )

    assert clients[0].book_calls == []
    assert result["pmus_acquisition"]["book_requests"] == 0
    failure = next(
        item for item in result["summary"]["rows"] if "scoring_error" in item
    )
    assert failure["scoring_error"] == type(unexpected).__name__
    assert not any("verdict" in item for item in result["summary"]["rows"])


@pytest.mark.parametrize("action", [Action.BUY, Action.WATCH, Action.PASS])
def test_streamed_equivalent_book_preserves_nfl_scoring_action(
    monkeypatch, tmp_path, action
):
    row = _pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[row],
        book=lambda _slug: (_ for _ in ()).throw(
            AssertionError("equivalent streamed input must avoid REST")
        ),
        action=action,
        stream_factory=lambda slugs: _FakePMUSStream(slugs),
    )

    verdicts = [
        item["verdict"]
        for item in result["summary"]["rows"]
        if "verdict" in item
    ]
    assert verdicts == [action.value, action.value]
    assert clients[0].book_calls == []


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


def test_nfl_multiple_candidates_reuse_cross_cycle_books(monkeypatch, tmp_path):
    rows = [
        _pmus_nfl_row("plain", "nfl-cin-pit-2026-09-27"),
        _pmus_nfl_row("aec", "aec-nfl-cin-pit-2026-09-27"),
    ]
    first, first_clients = _run_pmus_scan(
        monkeypatch, tmp_path, rows=rows, book=lambda slug: {"slug": slug}
    )
    second, second_clients = _run_pmus_scan(
        monkeypatch, tmp_path, rows=rows, book=lambda slug: {"slug": slug}
    )

    assert first_clients[0].book_calls == [row["slug"] for row in rows]
    assert second_clients[0].book_calls == []
    assert second["pmus_acquisition"]["cache_hits"] >= 3
    assert first["read_only"] is True and second["orders"] == 0


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


def test_indianapolis_no_and_washington_yes_are_one_washington_position(
    monkeypatch,
):
    game = NFLGame(
        "2026_04_IND_WAS",
        2026,
        "REG",
        "2026-10-04T13:30:00+00:00",
        "WAS",
        "IND",
        None,
        None,
    )
    observed = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    event = {
        "ticker": "KXNFLGAME-26OCT04INDWAS",
        "title": "IND vs WAS NFL game",
        "away_team": "IND",
        "home_team": "WAS",
        "scheduled_start": game.kickoff,
    }

    def contract(team):
        raw = {
            "ticker": f"KXNFLGAME-26OCT04INDWAS-{team}",
            "event_ticker": event["ticker"],
            "title": f"{team} wins",
            "yes_sub_title": team,
            "no_sub_title": team,
            "status": "open",
            "market_type": "binary",
            "expected_expiration_time": game.kickoff,
            "rules_primary": f"Resolves Yes if {team} wins the NFL game.",
            "away_team": "IND",
            "home_team": "WAS",
            "scheduled_start": game.kickoff,
        }
        return normalize_kalshi(raw, {}, observed.isoformat(), event=event)

    indianapolis_market = contract("IND")
    washington_market = contract("WAS")
    indianapolis = map_market_to_game(indianapolis_market, [game], now=observed)
    washington = map_market_to_game(washington_market, [game], now=observed)
    monkeypatch.setattr("parallax.nfl.probability_for_game", lambda *_args: 0.575486802364287)
    monkeypatch.setattr("parallax.nfl.utcnow", lambda: observed)

    indianapolis_yes = NFLEvidenceProvider(lambda: [game]).assess(
        indianapolis_market
    ).fair_probability
    washington_yes = NFLEvidenceProvider(lambda: [game]).assess(
        washington_market
    ).fair_probability

    assert indianapolis.selected_team == "IND"
    assert washington.selected_team == "WAS"
    assert economic_team_for_side(indianapolis, Side.YES) == "IND"
    assert economic_team_for_side(indianapolis, Side.NO) == "WAS"
    assert economic_team_for_side(washington, Side.YES) == "WAS"
    assert economic_team_for_side(washington, Side.NO) == "IND"

    assert indianapolis_yes == pytest.approx(0.424513197635713)
    assert washington_yes == pytest.approx(0.575486802364287)
    assert 1 - indianapolis_yes == pytest.approx(washington_yes)
    assert nfl_live_scan._nfl_economic_position(
        SimpleNamespace(side=Side.NO), indianapolis
    ) == ("NFL:2026_04_IND_WAS:WAS", "Washington Commanders")
    assert nfl_live_scan._nfl_economic_position(
        SimpleNamespace(side=Side.YES), washington
    ) == ("NFL:2026_04_IND_WAS:WAS", "Washington Commanders")


def _publication_candidate(
    now,
    *,
    market_id="contract-was",
    game_id="2026_04_IND_WAS",
    away_team="IND",
    home_team="WAS",
    contract_team="WAS",
    economic_team="WAS",
    selected_label="Washington Commanders",
    side=Side.YES,
    price=0.35,
    action=Action.BUY,
    freshness="FRESH",
    market_status="OPEN",
    venue=Venue.KALSHI,
):
    game = SimpleNamespace(
        game_id=game_id,
        away_team=away_team,
        home_team=home_team,
        kickoff="2026-10-04T13:30:00+00:00",
    )
    mapping = SimpleNamespace(
        status="MAPPED_GAME_WINNER",
        game=game,
        selected_team=contract_team,
    )
    play = SimpleNamespace(
        market_id=market_id,
        side=side,
        venue=venue,
        executable_price=price,
        model_probability=0.65,
        edge_points=22.54868024,
        fees_estimate=0.01,
        executable_size=100,
        expected_value=3.0,
        expected_return=0.1,
        suggested_action=action,
        verdict=SimpleNamespace(failed_gates=()),
        updated_at=now.isoformat(),
        expires_at=(now + timedelta(seconds=60)).isoformat(),
        data_freshness=freshness,
        status="CURRENT" if freshness == "FRESH" else "STALE",
        demo=False,
        evidence=object(),
    )
    market = SimpleNamespace(
        title=f"{contract_team} wins",
        venue_market_id=market_id,
        venue=venue,
        status=market_status,
    )
    return (
        play,
        market,
        mapping,
        now,
        f"NFL:{game_id}:{economic_team}",
        selected_label,
    )


def test_economic_duplicate_contracts_dispatch_once(monkeypatch):
    sent = []

    def dispatch(_dispatcher, play, _market, _mapping, _detected_at, **kwargs):
        sent.append((play.market_id, play.side, kwargs))
        return {"status": "SENT", "deduplicated": False}

    monkeypatch.setattr(nfl_live_scan, "_dispatch_buy_alert", dispatch)
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    washington_key = "NFL:2026_04_IND_WAS:WAS"
    candidates = [
        _publication_candidate(
            now,
            market_id="contract-ind",
            contract_team="IND",
            side=Side.NO,
            price=0.36,
        ),
        _publication_candidate(now, market_id="contract-was", price=0.35),
        _publication_candidate(
            now,
            market_id="contract-independent",
            game_id="another-game",
            away_team="KC",
            home_team="LV",
            contract_team="KC",
            economic_team="KC",
            selected_label="Kansas City Chiefs",
            price=0.30,
        ),
    ]
    eligible = nfl_live_scan._publication_candidates(
        candidates,
        scheduled_game_ids={"2026_04_IND_WAS", "another-game"},
        blocked_game_ids=set(),
        now=now,
    )
    result = nfl_live_scan._dispatch_eligible_buy_alerts(object(), eligible)

    assert result == {"sent": 2, "deduplicated": 0, "failed": 0, "withheld": 0}
    assert [(market_id, side) for market_id, side, _kwargs in sent] == [
        ("contract-was", Side.YES),
        ("contract-independent", Side.YES),
    ]
    assert sent[0][2] == {
        "economic_key": washington_key,
        "selected_side": "Washington Commanders",
    }


def test_equivalent_pmus_and_kalshi_contracts_dispatch_once(monkeypatch):
    from parallax.public_feed import sanitize_completed_scan

    sent = []
    monkeypatch.setattr(
        nfl_live_scan,
        "_dispatch_buy_alert",
        lambda _dispatcher, play, *_args, **_kwargs: sent.append(play.market_id)
        or {"status": "SENT", "deduplicated": False},
    )
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    candidates = [
        _publication_candidate(now, market_id="kalshi-contract", price=0.36),
        _publication_candidate(
            now,
            market_id="pmus-contract",
            price=0.35,
            venue=Venue.POLYMARKET,
        ),
    ]
    eligible = nfl_live_scan._publication_candidates(
        candidates,
        scheduled_game_ids={"2026_04_IND_WAS"},
        blocked_game_ids=set(),
        now=now,
    )
    summary = nfl_live_scan._dispatch_eligible_buy_alerts(object(), eligible)
    public = sanitize_completed_scan(
        "nfl",
        json.dumps(
            {
                "slate": {
                    "market_data_complete": False,
                    "expected_games": 1,
                    "accounted_games": 1,
                    "all_games_accounted": True,
                    "dates": [{
                        "date": "2026-10-04",
                        "games": [{
                            "game_id": "2026_04_IND_WAS",
                            "away_team": "IND",
                            "home_team": "WAS",
                            "status": "BUY",
                        }],
                    }],
                },
                "summary": {"rows": [
                    {
                        "venue": "KALSHI", "market_id": "kalshi-contract",
                        "game_id": "2026_04_IND_WAS", "side": "YES",
                        "verdict": "BUY", "executable_price": 0.36,
                        "nfl_v1_probability": 0.65, "raw_edge": 22.55,
                        "safety_margin": 0.17, "fee": 0.01,
                        "net_ev_25": 3, "expected_return": 0.1,
                        "liquidity": 100, "failed_gates": [],
                        "mapping_status": "MAPPED_GAME_WINNER",
                        "acquisition_status": "ACQUIRED", "economic_key": "NFL:2026_04_IND_WAS:WAS",
                        "economic_team": "WAS", "selected_team": "Washington Commanders",
                        "updated_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=60)).isoformat(),
                        "data_freshness": "FRESH", "status": "CURRENT",
                        "game_start": "2026-10-04T13:30:00+00:00",
                        "publication_eligible": True,
                    },
                    {
                        "venue": "PMUS", "market_id": "pmus-contract",
                        "game_id": "2026_04_IND_WAS", "side": "YES",
                        "verdict": "BUY", "executable_price": 0.35,
                        "nfl_v1_probability": 0.65, "raw_edge": 22.55,
                        "safety_margin": 0.17, "fee": 0.01,
                        "net_ev_25": 3, "expected_return": 0.1,
                        "liquidity": 100, "failed_gates": [],
                        "mapping_status": "MAPPED_GAME_WINNER",
                        "acquisition_status": "ACQUIRED", "economic_key": "NFL:2026_04_IND_WAS:WAS",
                        "economic_team": "WAS", "selected_team": "Washington Commanders",
                        "updated_at": now.isoformat(),
                        "expires_at": (now + timedelta(seconds=60)).isoformat(),
                        "data_freshness": "FRESH", "status": "CURRENT",
                        "game_start": "2026-10-04T13:30:00+00:00",
                        "publication_eligible": True,
                    },
                ]},
            }
        ),
        generated_at=now,
    )

    assert summary["sent"] == 1
    assert sent == ["pmus-contract"]
    assert public["summary"]["buy"] == 1


@pytest.mark.parametrize("unrelated_failure", ["GB_TB", "JAX_CIN"])
def test_unrelated_game_failure_does_not_block_safe_buy(unrelated_failure):
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    candidate = _publication_candidate(now)

    assert nfl_live_scan._publication_candidates(
        [candidate],
        scheduled_game_ids={"2026_04_IND_WAS", unrelated_failure},
        blocked_game_ids={unrelated_failure},
        now=now,
    ) == [candidate]


@pytest.mark.parametrize("failure", ["MAPPING_FAILURE", "DATA_UNAVAILABLE"])
def test_buy_own_game_localized_failure_is_blocked(failure):
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    candidate = _publication_candidate(now)
    blocked_by_status = {failure: {"2026_04_IND_WAS"}}

    assert nfl_live_scan._publication_candidates(
        [candidate],
        scheduled_game_ids={"2026_04_IND_WAS"},
        blocked_game_ids=blocked_by_status[failure],
        now=now,
    ) == []


def test_stale_buy_is_not_publication_eligible():
    decision_at = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    actual_publication_at = decision_at + timedelta(seconds=61)
    candidate = _publication_candidate(decision_at)

    assert nfl_live_scan._publication_candidates(
        [candidate],
        scheduled_game_ids={"2026_04_IND_WAS"},
        blocked_game_ids=set(),
        now=actual_publication_at,
    ) == []


@pytest.mark.parametrize("action", [Action.WATCH, Action.PASS])
def test_watch_and_pass_are_not_publication_eligible(action):
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    candidate = _publication_candidate(now, action=action)

    assert nfl_live_scan._publication_candidates(
        [candidate],
        scheduled_game_ids={"2026_04_IND_WAS"},
        blocked_game_ids=set(),
        now=now,
    ) == []


def test_wrong_or_ambiguous_economic_side_is_blocked():
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    wrong_side = _publication_candidate(
        now,
        economic_team="IND",
        selected_label="Indianapolis Colts",
    )
    ambiguous = list(_publication_candidate(now))
    ambiguous[2] = SimpleNamespace(
        status="MAPPED_GAME_WINNER",
        game=ambiguous[2].game,
        selected_team=None,
    )

    assert nfl_live_scan._publication_candidates(
        [wrong_side, tuple(ambiguous)],
        scheduled_game_ids={"2026_04_IND_WAS"},
        blocked_game_ids=set(),
        now=now,
    ) == []


def test_incomplete_acquisition_for_buy_itself_is_blocked():
    now = datetime(2026, 10, 3, 11, 12, tzinfo=UTC)
    candidate = _publication_candidate(now, market_status="CLOSED")

    assert nfl_live_scan._publication_candidates(
        [candidate],
        scheduled_game_ids={"2026_04_IND_WAS"},
        blocked_game_ids=set(),
        now=now,
    ) == []


def test_complete_scored_nfl_buy_survives_scan_to_publication_contract(
    monkeypatch, tmp_path
):
    from parallax.public_feed import sanitize_completed_scan

    dispatched = []
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")],
        book=lambda _slug: (_ for _ in ()).throw(
            AssertionError("published streamed BUY must not call REST")
        ),
        action_by_side=lambda side: Action.BUY if side == Side.YES else Action.PASS,
        dispatched=dispatched,
        stream_factory=lambda slugs: _FakePMUSStream(slugs),
    )
    public = sanitize_completed_scan(
        "nfl",
        json.dumps(result),
        generated_at=datetime(2026, 9, 27, 12, 0, 30, tzinfo=UTC),
    )

    assert result["slate"]["market_data_complete"] is True
    assert result["alerts"] == 1
    assert len(dispatched) == 1
    assert public["buy_publication_eligible"] is True
    assert public["summary"]["buy"] == 1
    published = next(play for play in public["plays"] if play["action"] == "BUY")
    alerted_play, alerted_market, _mapping, _detected_at, alert_identity = dispatched[0]
    assert alerted_play.venue is Venue.POLYMARKET
    assert alerted_market.venue is Venue.POLYMARKET
    assert (
        published["venue"],
        published["market_id"],
        published["contract_side"],
        published["price"],
        published["model_probability"],
        published["edge_pp"],
    ) == (
        "PMUS",
        alerted_play.market_id,
        alerted_play.side.value,
        alerted_play.executable_price,
        alerted_play.model_probability,
        alerted_play.edge_points,
    )
    assert alert_identity == {
        "economic_key": "NFL:2026_04_CIN_PIT:CIN",
        "selected_side": "Cincinnati Bengals",
    }


def test_pmus_rate_limit_does_not_block_valid_kalshi_buy(monkeypatch, tmp_path):
    dispatched = []
    published = []

    def rate_limited(_slug):
        raise PolymarketUSRateLimit("public REST rate limited")

    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("pmus-failure", "nfl-cin-pit-2026-09-27")],
        book=rate_limited,
        kalshi_rows=[_kalshi_nfl_row()],
        action_by_market_side=lambda market, side: (
            Action.BUY
            if market.venue is Venue.KALSHI and side is Side.YES
            else Action.PASS
        ),
        dispatched=dispatched,
        fast_publisher=published.append,
    )

    assert any(
        row.get("scoring_error") == "RATE_LIMITED"
        for row in result["summary"]["rows"]
    )
    assert result["slate"]["market_data_complete"] is False
    assert result["alerts"] == 1
    assert [entry[0].venue for entry in dispatched] == [Venue.KALSHI]
    assert next(
        row
        for row in result["summary"]["rows"]
        if row.get("verdict") == "BUY"
    )["publication_eligible"] is True
    assert published[0]["slate"]["dates"][0]["games"][0]["status"] == "BUY"


def test_pmus_acquisition_avoided_does_not_block_valid_kalshi_buy(
    monkeypatch, tmp_path
):
    class AvoidingAcquisition:
        def __init__(self):
            self.metrics = {"discovery_requests": 0}

        def discover(self, _key, request):
            self.metrics["discovery_requests"] += 1
            return request()

        def book(self, *_args, **_kwargs):
            return SimpleNamespace(
                book=None,
                observed_at=None,
                avoided_reason="test acquisition budget",
            )

        def diagnostics(self):
            return {"requests_avoided": 1}

    dispatched = []
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("pmus-avoided", "nfl-cin-pit-2026-09-27")],
        book=lambda _slug: {},
        kalshi_rows=[_kalshi_nfl_row()],
        action_by_market_side=lambda market, side: (
            Action.BUY
            if market.venue is Venue.KALSHI and side is Side.YES
            else Action.PASS
        ),
        dispatched=dispatched,
        pmus_acquisition=AvoidingAcquisition(),
    )

    assert any(
        row.get("scoring_error") == "ACQUISITION_AVOIDED"
        for row in result["summary"]["rows"]
    )
    assert result["slate"]["market_data_complete"] is False
    assert result["alerts"] == 1
    assert [entry[0].venue for entry in dispatched] == [Venue.KALSHI]
    assert next(
        row
        for row in result["summary"]["rows"]
        if row.get("verdict") == "BUY"
    )["publication_eligible"] is True


def test_pmus_evidence_failure_does_not_block_valid_kalshi_buy(
    monkeypatch, tmp_path
):
    dispatched = []
    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("pmus-no-evidence", "nfl-cin-pit-2026-09-27")],
        book=lambda _slug: {},
        kalshi_rows=[_kalshi_nfl_row()],
        evidence_for_market=lambda market: (
            None
            if market.venue is Venue.POLYMARKET
            else SimpleNamespace(fair_probability=0.5)
        ),
        action_by_market_side=lambda market, side: (
            Action.BUY
            if market.venue is Venue.KALSHI and side is Side.YES
            else Action.PASS
        ),
        dispatched=dispatched,
    )

    assert clients[0].book_calls == []
    assert result["summary"]["status_counts"]["EVIDENCE_MISSING"] == 1
    assert result["slate"]["market_data_complete"] is False
    assert result["alerts"] == 1
    assert [entry[0].venue for entry in dispatched] == [Venue.KALSHI]
    assert next(
        row
        for row in result["summary"]["rows"]
        if row.get("verdict") == "BUY"
    )["publication_eligible"] is True


def test_kalshi_failure_does_not_block_valid_pmus_buy(monkeypatch, tmp_path):
    dispatched = []
    published = []
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("pmus-buy", "nfl-cin-pit-2026-09-27")],
        book=lambda _slug: {"book": "available"},
        kalshi_rows=[_kalshi_nfl_row("kalshi-failure")],
        kalshi_book=lambda _ticker: (_ for _ in ()).throw(
            RuntimeError("Kalshi book unavailable")
        ),
        action_by_market_side=lambda market, side: (
            Action.BUY
            if market.venue is Venue.POLYMARKET and side is Side.YES
            else Action.PASS
        ),
        dispatched=dispatched,
        fast_publisher=published.append,
    )

    assert any(
        row.get("scoring_error") == "RuntimeError"
        for row in result["summary"]["rows"]
    )
    assert result["slate"]["market_data_complete"] is False
    assert result["alerts"] == 1
    assert [entry[0].venue for entry in dispatched] == [Venue.POLYMARKET]
    assert next(
        row
        for row in result["summary"]["rows"]
        if row.get("verdict") == "BUY"
    )["publication_eligible"] is True
    assert published[0]["slate"]["dates"][0]["games"][0]["status"] == "BUY"


def test_game_wide_mapping_ambiguity_still_blocks_peer_venue_buy(
    monkeypatch, tmp_path
):
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

    def mapping(market):
        return SimpleNamespace(
            status=(
                "AMBIGUOUS"
                if market.venue is Venue.KALSHI
                else "MAPPED_GAME_WINNER"
            ),
            reason="conflicting economic identity",
            game=game,
            selected_team=None if market.venue is Venue.KALSHI else "CIN",
        )

    dispatched = []
    published = []
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("pmus-buy", "nfl-cin-pit-2026-09-27")],
        book=lambda _slug: {"book": "available"},
        kalshi_rows=[_kalshi_nfl_row("kalshi-ambiguous")],
        action_by_market_side=lambda market, side: (
            Action.BUY
            if market.venue is Venue.POLYMARKET and side is Side.YES
            else Action.PASS
        ),
        dispatched=dispatched,
        scan_games=[game],
        mapping_for_market=mapping,
        fast_publisher=published.append,
    )

    assert result["summary"]["status_counts"]["AMBIGUOUS"] == 1
    assert result["slate"]["market_data_complete"] is False
    assert result["alerts"] == 0
    assert dispatched == []
    assert next(
        row
        for row in result["summary"]["rows"]
        if row.get("verdict") == "BUY"
    )["publication_eligible"] is False
    assert published[0]["slate"]["dates"][0]["games"][0]["status"] == "WITHHELD_INELIGIBLE"


def test_degraded_scan_with_individually_safe_buy_still_publishes(
    monkeypatch, tmp_path
):
    from parallax.public_feed import sanitize_completed_scan

    dispatched = []
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[_pmus_nfl_row("825205", "nfl-cin-pit-2026-09-27")],
        book=lambda slug: {"slug": slug},
        action_by_side=lambda side: Action.BUY if side == Side.YES else Action.PASS,
        dispatched=dispatched,
        kalshi_coverage_state="PARTIAL",
    )
    public = sanitize_completed_scan(
        "nfl",
        json.dumps(result),
        generated_at=datetime(2026, 9, 27, 12, 0, 30, tzinfo=UTC),
    )

    assert any(
        row.get("verdict") == "BUY" for row in result["summary"]["rows"]
    )
    assert result["slate"]["market_data_complete"] is False
    assert result["alerts"] == 1
    assert len(dispatched) == 1
    assert result["summary"]["status_counts"]["BUY_ALERT_WITHHELD_INELIGIBLE"] == 0
    assert public["data_quality_state"] == "DEGRADED"
    assert public["market_data_complete"] is False
    assert public["buy_publication_eligible"] is True
    assert public["summary"]["buy"] == 1
    assert next(play for play in public["plays"] if play["action"] == "BUY")[
        "publication_eligible"
    ] is True


def test_nfl_game_buy_finalizes_before_unrelated_slow_game(monkeypatch, tmp_path):
    """A slow unrelated game cannot consume a valid BUY's publication window."""
    t0 = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    game_a = NFLGame(
        "game-a", 2026, "REG", "2026-09-28T00:20:00+00:00", "PIT", "CIN", None, None
    )
    game_b = NFLGame(
        "game-b", 2026, "REG", "2026-09-28T00:20:00+00:00", "ARI", "LAC", None, None
    )
    rows = [
        _pmus_nfl_row("a-pmus", "nfl-cin-pit-2026-09-27"),
        _pmus_nfl_row("b-pmus", "nfl-lac-ari-2026-09-27"),
    ]
    clock = [t0]
    dispatched = []
    published = []

    def book(slug):
        if slug.endswith("lac-ari-2026-09-27"):
            clock[0] += timedelta(seconds=61)
            raise RuntimeError("unrelated game book failed")
        return {"slug": slug}

    result, clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=rows,
        book=book,
        action_by_market_side=lambda market, side: (
            Action.BUY
            if market.venue_market_id == "a-pmus" and side == Side.YES
            else Action.PASS
        ),
        dispatched=dispatched,
        scan_games=[game_a, game_b],
        scan_clock=clock,
        fast_publisher=lambda payload: published.append((clock[0], payload)),
    )
    assert len(clients) == 1
    assert result["alerts"] == 1
    assert [entry[0].market_id for entry in dispatched] == ["a-pmus"]
    assert result["summary"]["status_counts"].get("BUY_ALERT_WITHHELD_INELIGIBLE", 0) == 0
    assert result["slate"]["market_data_complete"] is False
    game_a_publication = next(
        item for item in published if item[1]["slate"]["dates"][0]["games"][0]["game_id"] == "game-a"
    )
    assert game_a_publication[0] == t0
    assert game_a_publication[1]["summary"]["rows"][0]["publication_eligible"] is True
    # Fast publication consumes no acquisition method; provider counts stay at
    # the same one discovery request and two pre-existing per-market books.
    assert len(clients) == 1
    assert clients[0].book_calls == [
        "nfl-cin-pit-2026-09-27",
        "nfl-lac-ari-2026-09-27",
    ]


def test_nfl_later_own_venue_failure_blocks_buy_before_finalization(
    monkeypatch, tmp_path
):
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    game_a = NFLGame(
        "game-a", 2026, "REG", "2026-09-28T00:20:00+00:00", "PIT", "CIN", None, None
    )

    def book(slug):
        if slug.endswith("aec-nfl-cin-pit-2026-09-27"):
            raise RuntimeError("later equivalent contract book failed")
        return {"slug": slug}

    published = []
    result, _clients = _run_pmus_scan(
        monkeypatch,
        tmp_path,
        rows=[
            _pmus_nfl_row("a-pmus", "nfl-cin-pit-2026-09-27"),
            _pmus_nfl_row("a-pmus-failure", "aec-nfl-cin-pit-2026-09-27"),
        ],
        book=book,
        action_by_market_side=lambda market, side: (
            Action.BUY if market.venue_market_id == "a-pmus" and side == Side.YES
            else Action.PASS
        ),
        scan_games=[game_a],
        scan_clock=[now],
        fast_publisher=published.append,
    )

    assert result["alerts"] == 0
    assert result["summary"]["status_counts"]["BUY_ALERT_WITHHELD_INELIGIBLE"] == 1
    assert next(
        row
        for row in result["summary"]["rows"]
        if row.get("verdict") == "BUY"
    )["publication_eligible"] is False
    assert published[0]["slate"]["dates"][0]["games"][0]["status"] == "WITHHELD_INELIGIBLE"


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


def test_current_incremental_buy_kicks_existing_nfl_publisher_once(tmp_path, monkeypatch):
    import runpy
    from datetime import UTC, datetime
    from parallax.public_feed import publish_incremental_nfl_game

    helpers = runpy.run_path(
        str(Path(__file__).with_name("test_parallax_public_feed.py"))
    )
    buy = helpers["eligible_nfl_buy"](
        game_id="2026_03_ARI_NYG",
        market_id="KXNFLGAME-ARI-NYG-ARI",
    )
    payload = {
        "read_only": True,
        "orders": 0,
        "published": 0,
        "finalized_at": "2026-09-24T12:29:40+00:00",
        "slate": helpers["nfl_slate_for"](
            game_id="2026_03_ARI_NYG",
            status="BUY",
            complete=False,
        ),
        "summary": {"rows": [buy]},
    }

    destination = publish_incremental_nfl_game(
        payload,
        tmp_path,
        generated_at=datetime(2026, 9, 24, 12, 29, 40, tzinfo=UTC),
    )
    public = json.loads(destination.read_text(encoding="utf-8"))

    assert len(public["plays"]) == 1
    assert public["plays"][0]["action"] == "BUY"
    assert public["plays"][0]["publication_eligible"] is True
    assert public["plays"][0]["market_id"] == "KXNFLGAME-ARI-NYG-ARI"
    assert "game_id" not in public["plays"][0]

    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(nfl_live_scan.subprocess, "run", fake_run)

    assert nfl_live_scan._kick_nfl_publisher_for_current_buy(
        payload, destination
    ) is True
    assert calls == [
        (
            [
                "launchctl",
                "kickstart",
                "system/com.swarmaxis.parallax-nfl-publisher",
            ],
            {"check": True, "timeout": 15},
        )
    ]


def test_incremental_without_current_buy_does_not_kick_publisher(tmp_path, monkeypatch):
    destination = tmp_path / "nfl.json"
    destination.write_text(
        json.dumps(
            {
                "plays": [
                    {
                        "action": "BUY",
                        "publication_eligible": True,
                        "market_id": "KXNFLGAME-OTHER-GAME-TEAM",
                    }
                ]
            }
        )
    )
    payload = {
        "summary": {
            "rows": [
                {
                    "game_id": "2026_04_IND_WAS",
                    "market_id": "KXNFLGAME-26OCT04INDWAS-WAS",
                }
            ]
        }
    }

    def unexpected_run(*_args, **_kwargs):
        raise AssertionError(
            "publisher must not be kicked for an unrelated existing BUY"
        )

    monkeypatch.setattr(nfl_live_scan.subprocess, "run", unexpected_run)

    assert nfl_live_scan._kick_nfl_publisher_for_current_buy(
        payload, destination
    ) is False


@pytest.mark.parametrize(
    ("away", "home", "selected", "title"),
    [
        ("ARI", "NYG", "NYG", "New York G wins"),
        ("ARI", "NYG", "ARI", "Arizona wins"),
        ("GB", "TB", "GB", "Green Bay wins"),
        ("GB", "TB", "TB", "Tampa Bay wins"),
        ("LA", "PHI", "LA", "Los Angeles wins"),
        ("LA", "PHI", "PHI", "Philadelphia wins"),
        ("NYJ", "CHI", "NYJ", "New York J wins"),
        ("KC", "LV", "KC", "Kansas City wins"),
        ("KC", "LV", "LV", "Las Vegas wins"),
    ],
)
def test_kalshi_nfl_ticker_suffix_resolves_selected_team_when_title_is_abbreviated(
    away, home, selected, title
):
    observed = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    kickoff = "2026-10-05T20:00:00+00:00"
    event_ticker = f"KXNFLGAME-26OCT05{away}{home}"

    raw = {
        "ticker": f"{event_ticker}-{selected}",
        "event_ticker": event_ticker,
        "title": title,
        "yes_sub_title": "YES",
        "no_sub_title": "NO",
        "status": "open",
        "market_type": "binary",
        "expected_expiration_time": kickoff,
        "rules_primary": "Contract resolves from the official NFL result.",
        "away_team": away,
        "home_team": home,
        "scheduled_start": kickoff,
    }
    event = {
        "ticker": event_ticker,
        "title": f"{away} vs {home} NFL game",
        "away_team": away,
        "home_team": home,
        "scheduled_start": kickoff,
    }

    market = normalize_kalshi(raw, {}, observed.isoformat(), event=event)
    game = NFLGame("game", 2026, "REG", kickoff, home, away, None, None)

    mapping = map_market_to_game(market, [game], now=observed)

    assert mapping.status == "MAPPED_GAME_WINNER"
    assert mapping.selected_team == selected


def test_kalshi_nfl_ticker_suffix_not_in_official_game_fails_closed():
    observed = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    kickoff = "2026-10-05T20:00:00+00:00"
    event_ticker = "KXNFLGAME-26OCT05ARINYG"

    raw = {
        "ticker": f"{event_ticker}-XXX",
        "event_ticker": event_ticker,
        "title": "Unknown wins",
        "yes_sub_title": "YES",
        "no_sub_title": "NO",
        "status": "open",
        "market_type": "binary",
        "expected_expiration_time": kickoff,
        "rules_primary": "Contract resolves from the official NFL result.",
        "away_team": "ARI",
        "home_team": "NYG",
        "scheduled_start": kickoff,
    }
    event = {
        "ticker": event_ticker,
        "title": "ARI vs NYG NFL game",
        "away_team": "ARI",
        "home_team": "NYG",
        "scheduled_start": kickoff,
    }

    market = normalize_kalshi(raw, {}, observed.isoformat(), event=event)
    game = NFLGame("game", 2026, "REG", kickoff, "NYG", "ARI", None, None)

    mapping = map_market_to_game(market, [game], now=observed)

    assert mapping.status == "AMBIGUOUS"
    assert mapping.selected_team is None
