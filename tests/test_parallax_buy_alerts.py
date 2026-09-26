from dataclasses import replace
import sqlite3
from types import SimpleNamespace

import pytest

from parallax.alerts import (
    AlertConfig,
    AlertDeliveryStore,
    AlertDispatcher,
    DeliveryResult,
    dispatch_scored_buy,
    reconcile_active_buy_alerts,
)
from parallax.models import Action, Confidence, Mechanics, NormalizedMarket, Side, Venue


class FakeTransport:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def post_json(self, url, payload):
        self.calls.append((url, payload))
        return self.results.pop(0) if self.results else DeliveryResult("SENT", http_status=200)


def market():
    return NormalizedMarket(
        venue=Venue.KALSHI,
        venue_market_id="KXNFLGAME-TEST-KC",
        slug="KXNFLGAME-TEST-KC",
        title="Baltimore at Kansas City",
        description="Kansas City wins",
        category="NFL",
        event="KXNFLGAME-TEST",
        outcomes={"YES": "Kansas City", "NO": "Baltimore"},
        resolution_rules="Resolves YES if Kansas City wins.",
        resolution_time="2026-09-28T00:20:00+00:00",
        status="OPEN",
        yes_bid=0.43,
        yes_ask=0.44,
        no_bid=0.55,
        no_ask=0.56,
        best_bid_size=400,
        best_ask_size=325,
        executable_depth={"YES": ((0.44, 325),)},
        recent_volume=1200,
        recent_trade_count=40,
        last_trade_time="2026-09-23T15:00:00+00:00",
        book_timestamp="2026-09-23T15:00:00+00:00",
        data_timestamp="2026-09-23T15:00:00+00:00",
        source_url="https://kalshi.com/markets/KXNFLGAME-TEST-KC",
        mechanics=Mechanics(),
    )


def play(action=Action.BUY, *, price=0.44):
    return SimpleNamespace(
        suggested_action=action,
        venue=Venue.KALSHI,
        market_id="KXNFLGAME-TEST-KC",
        side=Side.YES,
        side_description="Kansas City",
        market_title="Baltimore at Kansas City",
        market_url="https://kalshi.com/markets/KXNFLGAME-TEST-KC",
        executable_price=price,
        model_probability=0.61,
        edge_points=17.0,
        executable_size=325.0,
        confidence_band=Confidence.HIGH,
        demo=False,
        updated_at="2026-09-23T15:01:02+00:00",
        resolution_time="2026-09-28T00:20:00+00:00",
    )


def dispatcher(tmp_path, transport):
    return AlertDispatcher(
        AlertDeliveryStore(tmp_path / "alerts.sqlite"),
        AlertConfig(
            mode="ntfy",
            ntfy_topic="configured-test-topic",
            ntfy_server="https://ntfy.example.test",
        ),
        transport,
    )


def send(alert_dispatcher, scored_play):
    return dispatch_scored_buy(
        alert_dispatcher,
        scored_play,
        market(),
        sport="NFL",
        matchup="Baltimore at Kansas City",
        detected_at="2026-09-23T15:01:02+00:00",
    )


def test_fresh_buy_dispatches_exactly_one_complete_ntfy_alert(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    result = send(alert_dispatcher, play())

    assert result == {
        "status": "SENT",
        "deduplicated": False,
        "http_status": 200,
        "error_code": None,
    }
    assert len(transport.calls) == 1
    url, payload = transport.calls[0]
    assert url == "https://ntfy.example.test/"
    assert payload["title"] == "PARALLAX BUY"
    assert payload["priority"] == 5
    assert payload["topic"] == "configured-test-topic"
    for expected in (
        "PARALLAX BUY",
        "Sport: NFL",
        "Venue: KALSHI",
        "Matchup: Baltimore at Kansas City",
        "Selected side: Kansas City",
        "Executable price: 44.0¢",
        "Model probability: 61.0%",
        "Edge: 17.0 pp",
        "Liquidity: 325 contracts",
        "Contract: https://kalshi.com/markets/KXNFLGAME-TEST-KC",
        "Timestamp: 2026-09-23T15:01:02+00:00",
    ):
        assert expected in payload["message"]
    assert alert_dispatcher.status()["sent"] == 1


def test_identical_buy_dedupes_but_material_price_change_realerts(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    first = send(alert_dispatcher, play())
    duplicate = send(alert_dispatcher, play())
    changed = send(alert_dispatcher, play(price=0.46))

    assert first["deduplicated"] is False
    assert duplicate["deduplicated"] is True
    assert changed["deduplicated"] is False
    assert len(transport.calls) == 2
    assert alert_dispatcher.status()["sent"] == 2


@pytest.mark.parametrize("action", [Action.WATCH, Action.PASS])
def test_non_buy_verdict_is_silent(tmp_path, action):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    assert send(alert_dispatcher, play(action)) is None
    assert transport.calls == []
    assert alert_dispatcher.status()["sent"] == 0


def test_ntfy_failure_is_logged_in_delivery_health_and_does_not_raise(
    tmp_path, caplog
):
    transport = FakeTransport(
        DeliveryResult(
            "FAILED",
            http_status=503,
            error_code="HTTP_5XX",
            error_summary="ntfy returned HTTP 503.",
        )
    )
    alert_dispatcher = dispatcher(tmp_path, transport)

    result = send(alert_dispatcher, play())
    scan_continued = True

    assert scan_continued is True
    assert result["status"] == "PENDING"
    assert result["http_status"] == 503
    assert result["error_code"] == "HTTP_5XX"
    assert alert_dispatcher.status()["pending"] == 1
    recent = alert_dispatcher.recent()[0]
    assert recent["http_status"] == 503
    assert recent["error_code"] == "HTTP_5XX"
    assert "PARALLAX ntfy delivery status=FAILED" in caplog.text


def test_ntfy_transport_exception_is_contained_and_failed(tmp_path):
    class RaisingTransport:
        def post_json(self, _url, _payload):
            raise RuntimeError("network stack failed")

    alert_dispatcher = dispatcher(tmp_path, RaisingTransport())

    result = send(alert_dispatcher, play())

    assert result["status"] == "FAILED"
    assert result["error_code"] == "TRANSPORT_EXCEPTION"
    assert alert_dispatcher.status()["failed"] == 1



def test_mlb_scan_dispatches_only_fresh_buy_alerts(monkeypatch):
    import parallax.__main__ as parallax_main

    buy = play(Action.BUY)
    watch = play(Action.WATCH)
    fake_market = market()
    calls = []

    def fake_dispatch(dispatcher, scored_play, scored_market, **kwargs):
        calls.append((dispatcher, scored_play, scored_market, kwargs))
        return {
            "status": "SENT",
            "deduplicated": False,
            "http_status": 200,
            "error_code": None,
        }

    monkeypatch.setattr(parallax_main, "dispatch_scored_buy", fake_dispatch)
    service = SimpleNamespace(
        markets=[fake_market],
        alert_dispatcher=object(),
        _plays=lambda: [buy, watch],
    )

    result = parallax_main.dispatch_scan_buy_alerts(service, sport="MLB")

    assert result == {"sent": 1, "deduplicated": 0, "failed": 0}
    assert len(calls) == 1
    assert calls[0][1] is buy
    assert calls[0][2] is fake_market
    assert calls[0][3]["sport"] == "MLB"


def test_mlb_buy_alert_failure_does_not_fail_scan(monkeypatch):
    import parallax.__main__ as parallax_main

    monkeypatch.setattr(
        parallax_main,
        "dispatch_scored_buy",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ntfy unavailable")),
    )
    service = SimpleNamespace(
        markets=[market()],
        alert_dispatcher=object(),
        _plays=lambda: [play(Action.BUY)],
    )

    assert parallax_main.dispatch_scan_buy_alerts(service, sport="MLB") == {
        "sent": 0,
        "deduplicated": 0,
        "failed": 1,
    }


def test_delivered_buy_is_withdrawn_once_when_rescored_watch(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    first = send(alert_dispatcher, play(Action.BUY))
    watch = play(Action.WATCH)
    result = reconcile_active_buy_alerts(
        alert_dispatcher,
        [watch],
        sport="NFL",
        detected_at="2026-09-23T15:02:02+00:00",
    )
    repeated = reconcile_active_buy_alerts(
        alert_dispatcher,
        [watch],
        sport="NFL",
        detected_at="2026-09-23T15:03:02+00:00",
    )

    assert first["status"] == "SENT"
    assert result == {"withdrawn": 1, "expired": 0, "failed": 0}
    assert repeated == {"withdrawn": 0, "expired": 0, "failed": 0}
    assert len(transport.calls) == 2
    assert transport.calls[1][1]["title"] == "PARALLAX BUY WITHDRAWN"
    assert "Current verdict: WATCH" in transport.calls[1][1]["message"]


def test_missing_current_play_does_not_withdraw_before_game_start(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    dispatch_scored_buy(
        alert_dispatcher,
        play(Action.BUY),
        market(),
        sport="NFL",
        matchup="Baltimore at Kansas City",
        detected_at="2026-09-23T15:01:02+00:00",
        game_start="2026-09-28T00:20:00+00:00",
    )
    result = reconcile_active_buy_alerts(
        alert_dispatcher,
        [],
        sport="NFL",
        detected_at="2026-09-23T15:05:02+00:00",
    )

    assert result == {"withdrawn": 0, "expired": 0, "failed": 0}
    assert len(transport.calls) == 1


def test_active_buy_expires_once_when_scheduled_game_starts(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    dispatch_scored_buy(
        alert_dispatcher,
        play(Action.BUY),
        market(),
        sport="NFL",
        matchup="Baltimore at Kansas City",
        detected_at="2026-09-23T15:01:02+00:00",
        game_start="2026-09-23T15:02:00+00:00",
    )
    result = reconcile_active_buy_alerts(
        alert_dispatcher,
        [],
        sport="NFL",
        detected_at="2026-09-23T15:02:01+00:00",
    )
    repeated = reconcile_active_buy_alerts(
        alert_dispatcher,
        [],
        sport="NFL",
        detected_at="2026-09-23T15:03:01+00:00",
    )

    assert result == {"withdrawn": 0, "expired": 1, "failed": 0}
    assert repeated == {"withdrawn": 0, "expired": 0, "failed": 0}
    assert len(transport.calls) == 2
    assert transport.calls[1][1]["title"] == "PARALLAX BUY EXPIRED"


def test_withdrawn_buy_reactivation_sends_new_buy_even_same_material_state(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    send(alert_dispatcher, play(Action.BUY))
    reconcile_active_buy_alerts(
        alert_dispatcher,
        [play(Action.WATCH)],
        sport="NFL",
        detected_at="2026-09-23T15:02:02+00:00",
    )
    reactivated = send(alert_dispatcher, play(Action.BUY))

    assert reactivated["status"] == "SENT"
    assert reactivated["deduplicated"] is False
    assert len(transport.calls) == 3
    assert transport.calls[2][1]["title"] == "PARALLAX BUY"


def test_kickoff_expiry_wins_over_non_buy_rescore(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)

    dispatch_scored_buy(
        alert_dispatcher,
        play(Action.BUY),
        market(),
        sport="NFL",
        matchup="Baltimore at Kansas City",
        detected_at="2026-09-23T15:01:02+00:00",
        game_start="2026-09-23T15:02:00+00:00",
    )
    result = reconcile_active_buy_alerts(
        alert_dispatcher,
        [play(Action.WATCH)],
        sport="NFL",
        detected_at="2026-09-23T15:02:01+00:00",
    )

    assert result == {"withdrawn": 0, "expired": 1, "failed": 0}
    assert transport.calls[1][1]["title"] == "PARALLAX BUY EXPIRED"


def test_mlb_scan_lifecycle_wrapper_uses_current_scored_plays(monkeypatch):
    import parallax.__main__ as parallax_main

    plays = [play(Action.BUY), play(Action.WATCH)]
    calls = []

    def fake_reconcile(dispatcher, current, **kwargs):
        calls.append((dispatcher, current, kwargs))
        return {"withdrawn": 0, "expired": 0, "failed": 0}

    monkeypatch.setattr(parallax_main, "reconcile_active_buy_alerts", fake_reconcile)
    service = SimpleNamespace(
        alert_dispatcher=object(),
        _plays=lambda: plays,
    )

    result = parallax_main.reconcile_scan_buy_lifecycle(service, sport="MLB")

    assert result == {"withdrawn": 0, "expired": 0, "failed": 0}
    assert calls[0][1] == plays
    assert calls[0][2]["sport"] == "MLB"


def _mlb_market(contract_team, ticker):
    base = market()
    return replace(
        base,
        venue_market_id=ticker,
        slug=ticker,
        title=f"{contract_team} wins",
        description=f"{contract_team} wins",
        category="MLB",
        event="KXMLBGAME-26SEP25LADSF",
        outcomes={"YES": contract_team, "NO": contract_team},
        original_metadata={
            "market": {
                "mlb": {
                    "league": "MLB",
                    "market_type": "moneyline",
                    "away_team": "Los Angeles Dodgers",
                    "home_team": "San Francisco Giants",
                    "start_time": "2026-09-26T02:15:00Z",
                }
            }
        },
    )


def _mlb_play(market_id, side, label, price, edge):
    return SimpleNamespace(
        suggested_action=Action.BUY,
        venue=Venue.KALSHI,
        market_id=market_id,
        side=side,
        side_description=label,
        market_title=f"{label} wins",
        market_url=f"https://kalshi.com/markets/{market_id}",
        executable_price=price,
        model_probability=0.41534353117308787,
        edge_points=edge,
        executable_size=325.0,
        confidence_band=Confidence.HIGH,
        demo=False,
        updated_at="2026-09-25T18:38:05+00:00",
        resolution_time="2026-09-26T06:15:00+00:00",
    )


def test_mlb_economic_position_normalizes_cross_venue_team_aliases():
    import parallax.__main__ as parallax_main

    kalshi_market = replace(
        _mlb_market("Los Angeles Dodgers", "KAL-LAD"),
        original_metadata={
            "market": {
                "mlb": {
                    "league": "MLB",
                    "market_type": "moneyline",
                    "away_team": "Los Angeles Dodgers",
                    "home_team": "San Francisco",
                    "start_time": "2026-09-27T19:05:00Z",
                }
            }
        },
    )
    polymarket_market = replace(
        _mlb_market("San Francisco Giants", "POLY-SF"),
        venue=Venue.POLYMARKET,
        original_metadata={
            "market": {
                "mlb": {
                    "league": "MLB",
                    "market_type": "moneyline",
                    "away_team": "Los Angeles Dodgers",
                    "home_team": "San Francisco Giants",
                    "start_time": "2026-09-27T19:05:00Z",
                }
            }
        },
    )

    kalshi_play = _mlb_play("KAL-LAD", Side.NO, "Los Angeles D", 0.32, 9.5)
    polymarket_play = _mlb_play("POLY-SF", Side.YES, "San Francisco Giants", 0.33, 8.5)
    polymarket_play.venue = Venue.POLYMARKET

    service = SimpleNamespace(
        collection={
            "_market_game_ids": {
                "KALSHI:KAL-LAD": "823164",
                "POLYMARKET:POLY-SF": "823164",
            }
        }
    )

    kalshi_key, kalshi_team = parallax_main._mlb_economic_position(
        service, kalshi_play, kalshi_market
    )
    polymarket_key, polymarket_team = parallax_main._mlb_economic_position(
        service, polymarket_play, polymarket_market
    )

    assert kalshi_team == "San Francisco"
    assert polymarket_team == "San Francisco Giants"
    assert kalshi_key == polymarket_key == "MLB:823164:sanfranciscogiants"


def test_mlb_scan_collapses_equivalent_kalshi_buys_to_one_best_price(monkeypatch):
    import parallax.__main__ as parallax_main

    specs = [
        ("LAD-27", Side.NO, "Los Angeles D", 0.27, 14.53435312, "Los Angeles Dodgers"),
        ("SF-27", Side.YES, "San Francisco", 0.27, 14.53435312, "San Francisco Giants"),
        ("LAD-25", Side.NO, "Los Angeles D", 0.25, 16.53435312, "Los Angeles Dodgers"),
        ("SF-25", Side.YES, "San Francisco", 0.25, 16.53435312, "San Francisco Giants"),
        ("SF-33", Side.YES, "San Francisco", 0.33, 8.53435312, "San Francisco Giants"),
    ]
    markets = [_mlb_market(contract_team, market_id) for market_id, _side, _label, _price, _edge, contract_team in specs]
    plays = [_mlb_play(market_id, side, label, price, edge) for market_id, side, label, price, edge, _contract_team in specs]
    calls = []

    def fake_dispatch(dispatcher, scored_play, scored_market, **kwargs):
        calls.append((dispatcher, scored_play, scored_market, kwargs))
        return {
            "status": "SENT",
            "deduplicated": False,
            "http_status": 200,
            "error_code": None,
        }

    monkeypatch.setattr(parallax_main, "dispatch_scored_buy", fake_dispatch)
    service = SimpleNamespace(
        markets=markets,
        collection={
            "_market_game_ids": {
                f"KALSHI:{market.venue_market_id}": "823165"
                for market in markets
            }
        },
        alert_dispatcher=object(),
        _plays=lambda: plays,
    )

    result = parallax_main.dispatch_scan_buy_alerts(service, sport="MLB")

    assert result == {"sent": 1, "deduplicated": 0, "failed": 0}
    assert len(calls) == 1
    _dispatcher, selected_play, _market, kwargs = calls[0]
    assert selected_play.executable_price == 0.25
    assert kwargs["economic_key"] == "MLB:823165:sanfranciscogiants"
    assert kwargs["selected_side"] == "San Francisco Giants"


def test_economic_buy_identity_dedupes_contract_switch_and_keeps_one_active_state(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)
    first_market = _mlb_market("Los Angeles Dodgers", "LAD-NO")
    second_market = _mlb_market("San Francisco Giants", "SF-YES")
    first_play = _mlb_play("LAD-NO", Side.NO, "Los Angeles D", 0.25, 16.5)
    second_play = _mlb_play("SF-YES", Side.YES, "San Francisco", 0.25, 16.5)
    economic_key = "MLB:823165:sanfranciscogiants"

    first = dispatch_scored_buy(
        alert_dispatcher,
        first_play,
        first_market,
        sport="MLB",
        matchup="Los Angeles Dodgers at San Francisco Giants",
        detected_at=first_play.updated_at,
        economic_key=economic_key,
        selected_side="San Francisco Giants",
    )
    duplicate = dispatch_scored_buy(
        alert_dispatcher,
        second_play,
        second_market,
        sport="MLB",
        matchup="Los Angeles Dodgers at San Francisco Giants",
        detected_at=second_play.updated_at,
        economic_key=economic_key,
        selected_side="San Francisco Giants",
    )

    assert first["deduplicated"] is False
    assert duplicate["deduplicated"] is True
    assert len(transport.calls) == 1
    active = alert_dispatcher.store.active_buys("MLB")
    assert len(active) == 1
    assert active[0]["economic_key"] == economic_key
    assert active[0]["selected_side"] == "San Francisco Giants"


def test_economic_lifecycle_stays_active_when_equivalent_contract_is_still_buy(tmp_path):
    transport = FakeTransport()
    alert_dispatcher = dispatcher(tmp_path, transport)
    first_market = _mlb_market("Los Angeles Dodgers", "LAD-NO")
    first_play = _mlb_play("LAD-NO", Side.NO, "Los Angeles D", 0.25, 16.5)
    watch = _mlb_play("LAD-NO", Side.NO, "Los Angeles D", 0.40, 1.5)
    watch.suggested_action = Action.WATCH
    replacement = _mlb_play("SF-YES", Side.YES, "San Francisco", 0.26, 15.5)
    economic_key = "MLB:823165:sanfranciscogiants"

    dispatch_scored_buy(
        alert_dispatcher,
        first_play,
        first_market,
        sport="MLB",
        matchup="Los Angeles Dodgers at San Francisco Giants",
        detected_at=first_play.updated_at,
        economic_key=economic_key,
        selected_side="San Francisco Giants",
    )
    result = reconcile_active_buy_alerts(
        alert_dispatcher,
        [watch, replacement],
        sport="MLB",
        detected_at="2026-09-25T18:40:00+00:00",
        economic_key_for_play=lambda _play: economic_key,
    )

    assert result == {"withdrawn": 0, "expired": 0, "failed": 0}
    assert len(transport.calls) == 1


def test_schema_migration_supersedes_only_legacy_active_mlb_rows(tmp_path):
    path = tmp_path / "alerts.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE buy_alert_state (
                sport TEXT NOT NULL,
                venue TEXT NOT NULL,
                market_id TEXT NOT NULL,
                side TEXT NOT NULL,
                status TEXT NOT NULL,
                matchup TEXT NOT NULL,
                selected_side TEXT NOT NULL,
                activated_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                game_start TEXT,
                resolution_time TEXT,
                last_price REAL,
                model_probability REAL,
                edge_points REAL,
                closed_at TEXT,
                close_reason TEXT,
                PRIMARY KEY (sport, venue, market_id, side)
            )
            """
        )
        row = (
            "KALSHI",
            "market",
            "YES",
            "ACTIVE",
            "matchup",
            "team",
            "2026-09-25T18:00:00+00:00",
            "2026-09-25T18:00:00+00:00",
            None,
            None,
            0.25,
            0.40,
            15.0,
            None,
            None,
        )
        conn.execute(
            """
            INSERT INTO buy_alert_state (
                sport, venue, market_id, side, status, matchup, selected_side,
                activated_at, updated_at, game_start, resolution_time,
                last_price, model_probability, edge_points, closed_at, close_reason
            ) VALUES ('MLB', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )
        conn.execute(
            """
            INSERT INTO buy_alert_state (
                sport, venue, market_id, side, status, matchup, selected_side,
                activated_at, updated_at, game_start, resolution_time,
                last_price, model_probability, edge_points, closed_at, close_reason
            ) VALUES ('NFL', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            row,
        )

    store = AlertDeliveryStore(path)

    assert store.active_buys("MLB") == []
    assert len(store.active_buys("NFL")) == 1
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(buy_alert_state)")}
        mlb_status = conn.execute(
            "SELECT status FROM buy_alert_state WHERE sport = 'MLB'"
        ).fetchone()[0]
    assert "economic_key" in columns
    assert mlb_status == "SUPERSEDED"
