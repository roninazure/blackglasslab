from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from threading import Thread
from urllib.request import urlopen

import pytest

from parallax.api import server
from parallax.demo import demo_inputs
from parallax.entitlements import Plan
from parallax.models import SignalType, timestamp
from parallax.service import (
    OBSERVATION_HISTORY_LIMIT,
    SIGNAL_HISTORY_LIMIT,
    PlayService,
)
from parallax.track_record import TrackRecord


@pytest.fixture
def temporal(tmp_path):
    market = replace(demo_inputs()[0][0], demo=False)
    service = PlayService(TrackRecord(tmp_path / "record.sqlite"))
    return service, market


def later(market, seconds=90, **changes):
    observed = timestamp(market.data_timestamp) + timedelta(seconds=seconds)
    return replace(market, data_timestamp=observed.isoformat(), **changes)


def items_of_type(service, signal_type):
    return [
        row
        for row in service.signals(Plan.PRO)["items"]
        if row["signal_type"] == signal_type
    ]


def test_first_observation_creates_no_signal(temporal):
    service, market = temporal
    service.replace_inputs([market])
    assert service.signals(Plan.PRO)["items"] == []


def test_material_price_move_without_fair_value_or_order(temporal):
    service, market = temporal
    service.replace_inputs([market])
    service.replace_inputs([later(market, yes_ask=market.yes_ask + 0.07)])
    signals = items_of_type(service, SignalType.PRICE_MOVE)
    assert len(signals) == 1
    assert signals[0]["side"] == "YES"
    assert signals[0]["observation_window_seconds"] == 90
    assert "YES moved from" in signals[0]["explanation"]
    assert all(
        play["parallax_fair_value"] is None
        for play in service.plays(Plan.PRO)["items"]
    )
    assert service.alert_candidates(Plan.PRO) == []
    assert service.store.summary()["published_plays"] == 0
    assert service.health()["live_orders"] == 0


def test_tiny_noise_creates_no_signal(temporal):
    service, market = temporal
    service.replace_inputs([market])
    service.replace_inputs(
        [
            later(
                market,
                yes_ask=market.yes_ask + 0.01,
                yes_bid=market.yes_bid + 0.01,
                executable_depth={"YES": ((market.yes_ask + 0.01, 1050),)},
            )
        ]
    )
    assert service.signals(Plan.PRO)["items"] == []


@pytest.mark.parametrize(
    "bid,ask,direction",
    [(0.30, 0.33, "compressed"), (0.20, 0.32, "widened")],
)
def test_spread_move(temporal, bid, ask, direction):
    service, market = temporal
    baseline = replace(market, yes_bid=0.24, yes_ask=0.32)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=bid, yes_ask=ask)])
    signals = items_of_type(service, SignalType.SPREAD_MOVE)
    assert len(signals) == 1
    assert direction in signals[0]["explanation"]


def test_liquidity_move(temporal):
    service, market = temporal
    baseline = replace(market, executable_depth={"YES": ((0.32, 85),)})
    current = later(baseline, executable_depth={"YES": ((0.32, 390),)})
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signal = items_of_type(service, SignalType.LIQUIDITY_MOVE)[0]
    assert signal["previous_value"] == 85
    assert signal["current_value"] == 390
    assert signal["percent_change"] == pytest.approx(358.8235)


def test_signals_route_and_bounded_history(temporal):
    service, market = temporal
    for step in range(OBSERVATION_HISTORY_LIMIT + 3):
        service.replace_inputs([later(market, step * 90, yes_ask=0.32 + step * 0.06)])
    assert len(service.observation_history) == OBSERVATION_HISTORY_LIMIT
    assert len(service.observation_times) == OBSERVATION_HISTORY_LIMIT
    assert service.signal_history.maxlen == SIGNAL_HISTORY_LIMIT
    explorer = service.signals(Plan.EXPLORER)
    pro = service.signals(Plan.PRO)
    assert len(explorer["items"]) == explorer["limit"] == 5
    assert len(pro["items"]) == pro["total"] > explorer["limit"]

    api = server(service, port=0, resolve_plan=lambda headers: Plan.PRO)
    thread = Thread(target=api.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(f"http://127.0.0.1:{api.server_port}/signals") as response:
            payload = json.load(response)
        assert response.status == 200
        assert payload["items"]
        assert set(payload["items"][0]) >= {
            "id",
            "signal_type",
            "significance",
            "market_reference",
        }
    finally:
        api.shutdown()
        api.server_close()
        thread.join(timeout=2)
