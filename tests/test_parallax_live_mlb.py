from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import parallax.__main__ as parallax_main
from parallax import sources
from parallax.demo import demo_inputs
from parallax.inbox import InboxStore
from parallax.mlb import MLBStatsAPI
from parallax.models import Action, Evidence, Venue
from parallax.normalization import normalize_kalshi, rules_digest
from parallax.pmus_acquisition import PMUSAcquisition
from parallax.service import PlayService
from parallax.track_record import TrackRecord


def pmus_row(slug="aec-mlb-laa-wsh-2026-09-13"):
    raw = {
        "question": "Who will win Los Angeles Angels vs Washington Nationals?",
        "description": "Winner of the MLB baseball game.",
        "category": "sports",
        "marketType": "moneyline",
        "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE",
        "gameStartTime": "2026-09-13T17:35:00Z",
        "endDate": "2026-09-27T17:35:00Z",
        "marketSides": [
            {"long": True, "description": "Los Angeles Angels", "team": {"name": "Los Angeles Angels", "league": "mlb", "ordering": "away"}},
            {"long": False, "description": "Washington Nationals", "team": {"name": "Washington Nationals", "league": "mlb", "ordering": "home"}},
        ],
    }
    return {"raw": raw, "id": slug, "slug": slug, "question": raw["question"], "event_id": "E", "active": True, "closed": False, "accepting_orders": True}


def book(slug):
    return {
        f"{slug}::YES": {"best_bid": 0.45, "best_ask": 0.47, "bid_size_shares": 100, "ask_size_shares": 100},
        f"{slug}::NO": {"best_bid": 0.53, "best_ask": 0.55, "bid_size_shares": 100, "ask_size_shares": 100},
    }


class FakeSchedule:
    def scheduled_games_for_date(self, _target_date):
        return [
            {
                "game_id": f"game-{day}",
                "date": f"2026-09-{day}",
                "start_time": f"2026-09-{day}T17:35:00+00:00",
                "away_team": "Los Angeles Angels",
                "home_team": "Washington Nationals",
                "away_team_code": "LAA",
                "home_team_code": "WSH",
                "schedule_status": "SCHEDULED",
            }
            for day in ("13", "14")
        ]


def offline_acquisition(tmp_path):
    now = [datetime(2026, 9, 13, 12, tzinfo=UTC)]

    def advance(seconds):
        now[0] += timedelta(seconds=seconds)

    return PMUSAcquisition(
        "MLB",
        state_path=tmp_path / "pmus-acquisition.sqlite",
        clock=lambda: now[0],
        sleeper=advance,
    )


def test_mlb_official_schedule_preserves_doubleheaders_as_separate_games():
    payload = {
        "dates": [
            {
                "games": [
                    {
                        "gamePk": 1001,
                        "gameType": "R",
                        "gameDate": "2026-09-24T17:05:00Z",
                        "status": {"detailedState": "Scheduled"},
                        "teams": {
                            "away": {"team": {"name": "Milwaukee Brewers"}},
                            "home": {"team": {"name": "Philadelphia Phillies"}},
                        },
                    },
                    {
                        "gamePk": 1002,
                        "gameType": "R",
                        "gameDate": "2026-09-24T21:05:00Z",
                        "status": {"detailedState": "Scheduled"},
                        "teams": {
                            "away": {"team": {"name": "Milwaukee Brewers"}},
                            "home": {"team": {"name": "Philadelphia Phillies"}},
                        },
                    },
                ]
            }
        ]
    }
    api = MLBStatsAPI(transport=lambda _path: payload)

    slate = api.scheduled_games_for_date("2026-09-24")

    assert [row["game_id"] for row in slate] == ["1001", "1002"]
    assert all(row["date"] == "2026-09-24" for row in slate)


def test_mlb_official_schedule_preserves_supported_postseason_games():
    def game(game_id, game_type):
        return {
            "gamePk": game_id,
            "gameType": game_type,
            "gameDate": "2026-10-02T23:05:00Z",
            "status": {"detailedState": "Scheduled"},
            "teams": {
                "away": {"team": {"name": "Boston Red Sox"}},
                "home": {"team": {"name": "New York Yankees"}},
            },
        }

    payload = {
        "dates": [
            {
                "games": [
                    game(2001, "F"),
                    game(2002, "D"),
                    game(2003, "L"),
                    game(2004, "W"),
                    game(2005, "S"),
                ]
            }
        ]
    }

    slate = MLBStatsAPI(transport=lambda _path: payload).scheduled_games_for_date(
        "2026-10-02"
    )

    assert [(row["game_id"], row["game_type"]) for row in slate] == [
        ("2001", "F"),
        ("2002", "D"),
        ("2003", "L"),
        ("2004", "W"),
    ]


def test_mlb_game_for_market_accepts_supported_postseason_game_type(monkeypatch):
    calls = []
    target = {
        "gamePk": 2002,
        "gameType": "D",
        "gameDate": "2026-10-02T23:05:00Z",
        "status": {"abstractGameState": "Preview", "detailedState": "Scheduled"},
        "teams": {
            "away": {"team": {"id": 111, "name": "Boston Red Sox"}},
            "home": {"team": {"id": 147, "name": "New York Yankees"}},
        },
    }

    def transport(path):
        calls.append(path)
        if path.startswith("/standings?"):
            return {"records": []}
        return {"dates": [{"games": [target]}]}

    raw = {
        "ticker": "KXMLBGAME-26OCT02BOSNYY-NYY",
        "event_ticker": "KXMLBGAME-26OCT02BOSNYY",
        "title": "New York Yankees win",
        "yes_sub_title": "New York Yankees",
        "no_sub_title": "New York Yankees",
        "status": "active",
        "market_type": "binary",
        "rules_primary": (
            "If New York Yankees wins the Boston Red Sox vs New York Yankees "
            "professional baseball game originally scheduled for Oct 2, 2026 "
            "at 7:05 PM EDT, then the market resolves to Yes."
        ),
    }
    market = normalize_kalshi(raw, {}, "2026-10-01T12:00:00+00:00")
    monkeypatch.setattr(
        "parallax.mlb.utcnow", lambda: datetime(2026, 10, 1, 12, tzinfo=UTC)
    )

    mapped = MLBStatsAPI(transport=transport).game_for_market(market)

    assert mapped is not None
    assert mapped.game_id == "2002"
    assert mapped.game_type == "D"
    assert not any("startDate=" in path for path in calls)


def test_current_nested_pmus_metadata_is_mlb_moneyline():
    assert sources._is_mlb_moneyline(pmus_row())
    assert not sources._is_mlb_moneyline({"raw": {"marketType": "moneyline", "question": "NFL winner"}})


def test_bulk_mapping_precedes_book_and_isolates_book_failure(monkeypatch, tmp_path):
    first, second = pmus_row(), pmus_row("aec-mlb-laa-wsh-2026-09-14")
    calls = []
    discovery_calls = []

    class FakePMUS:
        def markets_page(self, *, limit, offset, slugs=None, retry_transport_errors=True):
            discovery_calls.append((limit, offset, slugs, retry_transport_errors))
            return [first, second] if offset == 0 else []

        def book(self, slug):
            calls.append(slug)
            if slug.endswith("09-14"):
                raise RuntimeError("rate limited")
            return book(slug)

        def close(self):
            pass

    class FakeKalshi:
        def mlb_markets_page(self, limit=100, cursor=""):
            return {"markets": []}

    class FakeProvider:
        def assess(self, market):
            assert market.yes_ask is None
            return Evidence(Venue.POLYMARKET, market.venue_market_id, 0.5, "test", "mlb-v2", "2026-09-13T12:00:00+00:00", "2026-09-13T13:00:00+00:00", rules_digest(market), "test", "test")

    monkeypatch.setattr(sources, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", FakeProvider)
    monkeypatch.setattr(sources, "MLBStatsAPI", FakeSchedule)
    markets, report = sources.collect_markets(
        limit=2, pmus_acquisition=offline_acquisition(tmp_path)
    )
    assert [m.slug for m in markets] == [first["slug"]]
    assert discovery_calls == [
        (
            4,
            0,
            [
                "mlb-laa-wsh-2026-09-13",
                "aec-mlb-laa-wsh-2026-09-13",
                "mlb-laa-wsh-2026-09-14",
                "aec-mlb-laa-wsh-2026-09-14",
            ],
            False,
        )
    ]
    assert calls == [first["slug"], second["slug"]]
    assert report["metrics"]["POLYMARKET.evidence_produced"] == 1
    assert any(error["stage"] == "market" for error in report["errors"])


def test_duplicate_pmus_market_gets_one_book_request(monkeypatch, tmp_path):
    row = pmus_row()
    calls = []

    class FakePMUS:
        def markets_page(self, *, limit, offset, slugs=None, retry_transport_errors=True):
            return [row, row] if offset == 0 else []

        def book(self, slug):
            calls.append(slug)
            return book(slug)

        def close(self):
            pass

    class FakeKalshi:
        def mlb_markets_page(self, limit=100, cursor=""):
            return {"markets": []}

    class FakeProvider:
        def assess(self, market):
            return Evidence(Venue.POLYMARKET, market.venue_market_id, 0.5, "test", "mlb-v2", "2026-09-13T12:00:00+00:00", "2026-09-13T13:00:00+00:00", rules_digest(market), "test", "test")

    monkeypatch.setattr(sources, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", FakeProvider)
    monkeypatch.setattr(sources, "MLBStatsAPI", FakeSchedule)
    sources.collect_markets(
        limit=2, pmus_acquisition=offline_acquisition(tmp_path)
    )
    assert calls == [row["slug"]]


def test_empty_authoritative_slate_skips_all_venue_traffic(monkeypatch, tmp_path):
    calls = Counter()

    class EmptySchedule:
        def scheduled_games_for_date(self, _target_date):
            return []

    class NoPMUS:
        def __init__(self):
            calls["pmus_client"] += 1

    class NoKalshi:
        def __init__(self):
            calls["kalshi_client"] += 1

    class NoEvidence:
        def __init__(self):
            calls["evidence_provider"] += 1

    monkeypatch.setattr(sources, "MLBStatsAPI", EmptySchedule)
    monkeypatch.setattr(sources, "PolymarketUSPublicClient", NoPMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", NoKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", NoEvidence)

    markets, collection = sources.collect_markets(
        pmus_acquisition=offline_acquisition(tmp_path)
    )

    assert markets == []
    assert calls == Counter()
    assert collection["errors"] == []
    assert collection["_slate_schedule_state"] == "COMPLETE"
    assert collection["_slate_discovery_complete"] is True
    assert collection["pmus_acquisition"]["discovery_requests"] == 0
    assert collection["pmus_acquisition"]["book_requests"] == 0
    assert collection["metrics"]["KALSHI.discovery_requests"] == 0
    report = parallax_main.mlb_slate_report(
        SimpleNamespace(collection=collection, _plays=list)
    )
    assert report["schedule_state"] == "COMPLETE"
    assert report["expected_games"] == 0
    assert report["market_data_complete"] is True


def test_unavailable_authoritative_slate_fails_closed_without_venue_traffic(
    monkeypatch, tmp_path
):
    calls = Counter()

    class UnavailableSchedule:
        def scheduled_games_for_date(self, _target_date):
            raise OSError("offline authoritative schedule failure")

    class NoPMUS:
        def __init__(self):
            calls["pmus_client"] += 1

    class NoKalshi:
        def __init__(self):
            calls["kalshi_client"] += 1

    monkeypatch.setattr(sources, "MLBStatsAPI", UnavailableSchedule)
    monkeypatch.setattr(sources, "PolymarketUSPublicClient", NoPMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", NoKalshi)

    markets, collection = sources.collect_markets(
        pmus_acquisition=offline_acquisition(tmp_path)
    )

    assert markets == []
    assert calls == Counter()
    assert collection["_slate_schedule_state"] == "DATA_UNAVAILABLE"
    assert collection["_slate_discovery_complete"] is False
    assert collection["pmus_acquisition"]["discovery_requests"] == 0
    assert collection["pmus_acquisition"]["book_requests"] == 0
    assert collection["metrics"]["KALSHI.discovery_requests"] == 0
    assert collection["errors"] == [
        {
            "venue": "OFFICIAL_MLB",
            "stage": "schedule",
            "error_type": "OSError",
        }
    ]
    report = parallax_main.mlb_slate_report(
        SimpleNamespace(collection=collection, _plays=list)
    )
    assert report["schedule_state"] == "DATA_UNAVAILABLE"
    assert report["expected_games"] is None
    assert report["market_data_complete"] is False


def test_kalshi_filters_non_slate_markets_before_event_series_and_book(
    monkeypatch, tmp_path
):
    calls = Counter()
    valid_event = "KXMLBGAME-26SEP29BOSNYY"
    unrelated_event = "KXMLBGAME-26SEP28BOSNYY"

    class OneGameSchedule:
        def scheduled_games_for_date(self, _target_date):
            return [
                {
                    "game_id": "123",
                    "date": "2026-09-29",
                    "start_time": "2026-09-29T23:05:00+00:00",
                    "away_team": "Boston Red Sox",
                    "home_team": "New York Yankees",
                    "away_team_code": "BOS",
                    "home_team_code": "NYY",
                    "schedule_status": "SCHEDULED",
                }
            ]

    class FakePMUS:
        def markets_page(
            self, *, limit, offset, slugs=None, retry_transport_errors=True
        ):
            calls["pmus_discovery"] += 1
            return []

        def book(self, _slug):
            calls["pmus_book"] += 1
            raise AssertionError("empty PMUS result cannot reach a book")

        def close(self):
            pass

    def kalshi_row(ticker, event_ticker, scheduled_for):
        return {
            "ticker": ticker,
            "event_ticker": event_ticker,
            "title": "New York Yankees win",
            "status": "active",
            "market_type": "binary",
            "yes_sub_title": "New York Yankees",
            "no_sub_title": "New York Yankees",
            "rules_primary": (
                "If New York Yankees wins the Boston Red Sox vs New York "
                "Yankees professional baseball game originally scheduled for "
                f"{scheduled_for} at 7:05 PM EDT, the market resolves to Yes."
            ),
            "rules_secondary": "The winner is the official full-game winner.",
            "price_level_structure": "linear_cent",
        }

    valid = kalshi_row(f"{valid_event}-NYY", valid_event, "Sep 29, 2026")
    unrelated = kalshi_row(f"{unrelated_event}-NYY", unrelated_event, "Sep 28, 2026")

    class FakeKalshi:
        def mlb_markets_page(self, limit=100, cursor=""):
            calls["kalshi_discovery"] += 1
            return {"markets": [valid, unrelated]}

        def event(self, ticker):
            calls[f"event:{ticker}"] += 1
            return {"event_ticker": ticker, "series_ticker": "KXMLBGAME"}

        def series(self, ticker):
            calls[f"series:{ticker}"] += 1
            return {
                "ticker": ticker,
                "fee_type": "quadratic",
                "fee_multiplier": 1,
            }

        def book(self, ticker):
            calls[f"book:{ticker}"] += 1
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.45", "10"]],
                    "no_dollars": [["0.50", "10"]],
                }
            }

    class FakeProvider:
        def assess(self, market):
            calls[f"evidence:{market.venue_market_id}"] += 1
            return Evidence(
                Venue.KALSHI,
                market.venue_market_id,
                0.55,
                "test",
                "mlb-v2",
                "2026-09-29T12:00:00+00:00",
                "2026-09-29T13:00:00+00:00",
                rules_digest(market),
                "official-mlb-statsapi:123",
                "test",
            )

    monkeypatch.setattr(sources, "MLBStatsAPI", OneGameSchedule)
    monkeypatch.setattr(sources, "PolymarketUSPublicClient", FakePMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", FakeProvider)

    markets, collection = sources.collect_markets(
        pmus_acquisition=offline_acquisition(tmp_path)
    )

    assert [market.venue_market_id for market in markets] == [valid["ticker"]]
    assert calls["kalshi_discovery"] == 1
    assert calls[f"event:{valid_event}"] == 1
    assert calls["series:KXMLBGAME"] == 1
    assert calls[f"book:{valid['ticker']}"] == 1
    assert calls[f"evidence:{valid['ticker']}"] == 1
    assert calls[f"event:{unrelated_event}"] == 0
    assert calls[f"book:{unrelated['ticker']}"] == 0
    assert calls[f"evidence:{unrelated['ticker']}"] == 0
    assert not any(
        error["stage"] == "mapping_or_evidence" for error in collection["errors"]
    )
    assert collection["metrics"]["KALSHI.markets_in_authoritative_slate"] == 1
    assert collection["metrics"]["KALSHI.markets_filtered_out"] == 1
    assert collection["_slate_discovery_complete"] is True


def test_postseason_slate_collects_only_schedule_matched_kalshi_moneylines(
    monkeypatch, tmp_path
):
    calls = Counter()
    event_ticker = "KXMLBGAME-26OCT02BOSNYY"

    class PostseasonSchedule:
        def scheduled_games_for_date(self, _target_date):
            return [
                {
                    "game_id": "2002",
                    "game_type": "D",
                    "date": "2026-10-02",
                    "start_time": "2026-10-02T23:05:00+00:00",
                    "away_team": "Boston Red Sox",
                    "home_team": "New York Yankees",
                    "away_team_code": "BOS",
                    "home_team_code": "NYY",
                    "schedule_status": "SCHEDULED",
                }
            ]

    class EmptyPMUS:
        def markets_page(self, **_kwargs):
            calls["pmus_discovery"] += 1
            return []

        def close(self):
            pass

    def row(ticker, event, scheduled_for):
        return {
            "ticker": ticker,
            "event_ticker": event,
            "title": "New York Yankees win",
            "yes_sub_title": "New York Yankees",
            "no_sub_title": "New York Yankees",
            "status": "active",
            "market_type": "binary",
            "rules_primary": (
                "If New York Yankees wins the Boston Red Sox vs New York Yankees "
                f"professional baseball game originally scheduled for {scheduled_for} "
                "at 7:05 PM EDT, then the market resolves to Yes."
            ),
            "rules_secondary": "The winner is the official full-game winner.",
            "price_level_structure": "linear_cent",
        }

    matched = row(f"{event_ticker}-NYY", event_ticker, "Oct 2, 2026")
    unrelated = row(
        "KXMLBGAME-26OCT03BOSNYY-NYY",
        "KXMLBGAME-26OCT03BOSNYY",
        "Oct 3, 2026",
    )

    class FakeKalshi:
        def mlb_markets_page(self, limit=100, cursor=""):
            calls["kalshi_discovery"] += 1
            return {"markets": [matched, unrelated]}

        def event(self, ticker):
            calls[f"event:{ticker}"] += 1
            return {"event_ticker": ticker, "series_ticker": "KXMLBGAME"}

        def series(self, ticker):
            calls[f"series:{ticker}"] += 1
            return {"ticker": ticker, "fee_type": "quadratic", "fee_multiplier": 1}

        def book(self, ticker):
            calls[f"book:{ticker}"] += 1
            return {
                "orderbook_fp": {
                    "yes_dollars": [["0.45", "100"]],
                    "no_dollars": [["0.50", "100"]],
                }
            }

    class PostseasonEvidence:
        def assess(self, market):
            calls[f"evidence:{market.venue_market_id}"] += 1
            return Evidence(
                Venue.KALSHI,
                market.venue_market_id,
                0.55,
                "test",
                "mlb-v2",
                "2026-10-02T12:00:00+00:00",
                "2026-10-02T13:00:00+00:00",
                rules_digest(market),
                "official-mlb-statsapi:2002",
                "postseason test",
                validation_reference="unvalidated:mlb-v2:official-postseason",
                source_independence="AUTHORITATIVE_PRIMARY",
                validation_status="UNVALIDATED",
            )

    monkeypatch.setattr(sources, "MLBStatsAPI", PostseasonSchedule)
    monkeypatch.setattr(sources, "PolymarketUSPublicClient", EmptyPMUS)
    monkeypatch.setattr(sources, "KalshiPublicClient", FakeKalshi)
    monkeypatch.setattr(sources, "MLBEvidenceProvider", PostseasonEvidence)
    monkeypatch.setattr(
        sources,
        "utcnow",
        lambda: datetime(2026, 10, 2, 12, tzinfo=UTC),
    )

    markets, collection = sources.collect_markets(
        pmus_acquisition=offline_acquisition(tmp_path)
    )

    assert [market.venue_market_id for market in markets] == [matched["ticker"]]
    assert calls["kalshi_discovery"] == 1
    assert calls[f"book:{matched['ticker']}"] == 1
    assert calls[f"book:{unrelated['ticker']}"] == 0
    assert calls[f"evidence:{matched['ticker']}"] == 1
    assert calls[f"evidence:{unrelated['ticker']}"] == 0
    assert collection["metrics"]["KALSHI.markets_in_authoritative_slate"] == 1
    assert collection["metrics"]["KALSHI.markets_filtered_out"] == 1


class SpyEvidenceEngine:
    def __init__(self):
        self.calls = 0

    def assess(self, market):
        self.calls += 1


def service_with_spy(tmp_path):
    spy = SpyEvidenceEngine()
    service = PlayService(
        TrackRecord(tmp_path / "record.sqlite"),
        inbox_store=InboxStore(tmp_path / "inbox.sqlite"),
        evidence_engine=spy,
    )
    service.refresh_inbox = lambda: None
    return service, spy


def test_collected_evidence_reaches_scoring_without_second_lookup(tmp_path):
    now = __import__("parallax.models", fromlist=["utcnow"]).utcnow()
    markets, evidence = demo_inputs(now)
    market = replace(markets[0], demo=False)
    proof = replace(evidence[(markets[0].venue, markets[0].venue_market_id)], demo=False)
    service, spy = service_with_spy(tmp_path)
    service.replace_inputs([market], collection={"_evidence": {(market.venue, market.venue_market_id): proof}})
    assert service.evidence[(market.venue, market.venue_market_id)] == proof
    assert spy.calls == 0
    play = service._plays()[0]
    assert play.evidence == proof
    assert play.edge_points is not None and play.expected_value is not None


def test_missing_collected_evidence_keeps_fail_closed_lookup(tmp_path):
    markets, _ = demo_inputs()
    market = replace(markets[0], demo=False)
    service, spy = service_with_spy(tmp_path)
    service.replace_inputs([market], collection={})
    assert spy.calls == 1
    assert service.evidence == {}


def _live_capture_workload(now, count=7):
    demo_markets, demo_evidence = demo_inputs(now)
    base = demo_markets[0]
    base_proof = demo_evidence[(base.venue, base.venue_market_id)]
    markets = []
    evidence = {}
    for index in range(count):
        market = replace(
            base,
            demo=False,
            venue_market_id=f"mlb-live-{index}",
            slug=f"mlb-live-{index}",
            title=f"MLB live market {index}",
            event=f"mlb-event-{index}",
        )
        proof = replace(
            base_proof,
            demo=False,
            market_id=market.venue_market_id,
            rules_digest=rules_digest(market),
            validation_reference="" if index == 0 else base_proof.validation_reference,
        )
        markets.append(market)
        evidence[(market.venue, market.venue_market_id)] = proof
    return markets, evidence


def test_live_mlb_capture_is_once_per_side_per_service_invocation(tmp_path, monkeypatch):
    now = __import__("parallax.models", fromlist=["utcnow"]).utcnow()
    first_at = now.replace(microsecond=0)
    monkeypatch.setattr("parallax.service.utcnow", lambda: first_at)
    markets, evidence = _live_capture_workload(first_at)
    publications = TrackRecord(tmp_path / "publications.sqlite")
    prospective = TrackRecord(tmp_path / "prospective.sqlite")

    class NoAlerts:
        def dispatch(self, _items):
            raise AssertionError("capture-only mode must not dispatch alerts")

        def status(self):
            return {
                "mode": "disabled",
                "pending": 0,
                "sent": 0,
                "failed": 0,
                "unknown": 0,
                "last_delivery_at": None,
            }

    class NoSocial:
        def enqueue(self, _item):
            raise AssertionError("capture-only mode must not enqueue social content")

        def publish_pending(self):
            raise AssertionError("capture-only mode must not publish social content")

        def status(self):
            return {
                "mode": "disabled",
                "pending": 0,
                "dry_run": 0,
                "sent": 0,
                "failed": 0,
                "unknown": 0,
            }

    service = PlayService(
        publications,
        inbox_store=InboxStore(tmp_path / "inbox.sqlite"),
        alert_dispatcher=NoAlerts(),
        social_publisher=NoSocial(),
        prospective_store=prospective,
        capture_only=True,
    )
    service.replace_inputs(markets, evidence)
    health = service.health()
    assert service.plays()["total"] == 14
    assert len(service._plays()) == 14

    records = prospective.prospective_records()
    assert len(records) == 14
    assert {record["verdict"] for record in records} == {
        Action.BUY,
        Action.WATCH,
        Action.PASS,
    }
    assert publications.summary()["published_plays"] == 0
    assert health["live_orders"] == 0 and not health["execution_enabled"]

    later = first_at + __import__("datetime").timedelta(seconds=1)
    monkeypatch.setattr("parallax.service.utcnow", lambda: later)
    second = PlayService(
        publications,
        inbox_store=InboxStore(tmp_path / "inbox-2.sqlite"),
        alert_dispatcher=NoAlerts(),
        social_publisher=NoSocial(),
        prospective_store=prospective,
        capture_only=True,
    )
    second.replace_inputs(markets, evidence)
    second._plays()
    assert len(prospective.prospective_records()) == 28


def test_mlb_slate_report_accounts_for_every_scheduled_game():
    schedule = [
        {
            "game_id": f"game-{index}",
            "date": "2026-09-24",
            "start_time": f"2026-09-24T{17 + index:02d}:00:00+00:00",
            "away_team": f"A{index}",
            "home_team": f"H{index}",
            "schedule_status": "SCHEDULED",
        }
        for index in range(4)
    ]
    plays = [
        SimpleNamespace(
            venue=Venue.POLYMARKET,
            market_id="m0",
            suggested_action=Action.BUY,
        ),
        SimpleNamespace(
            venue=Venue.KALSHI,
            market_id="m1",
            suggested_action=Action.WATCH,
        ),
    ]
    service = SimpleNamespace(
        collection={
            "_slate_schedule": schedule,
            "_slate_schedule_state": "COMPLETE",
            "_slate_discovery_complete": True,
            "_market_game_ids": {
                "POLYMARKET:m0": "game-0",
                "KALSHI:m1": "game-1",
            },
            "_slate_data_unavailable_game_ids": ["game-2"],
        },
        _plays=lambda: plays,
    )

    report = parallax_main.mlb_slate_report(service)

    assert report["expected_games"] == 4
    assert report["accounted_games"] == 4
    assert report["all_games_accounted"] is True
    assert [row["status"] for row in report["dates"][0]["games"]] == [
        "BUY",
        "WATCH",
        "DATA_UNAVAILABLE",
        "NO_MARKET",
    ]
