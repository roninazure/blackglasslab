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
