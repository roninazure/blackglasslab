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


def all_signals(service):
    return service.signals(Plan.PRO)["items"]


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


def test_price_beats_spread_and_liquidity_in_same_window(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.24,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    current = later(
        baseline,
        yes_bid=0.20,
        yes_ask=0.39,
        executable_depth={"YES": ((0.39, 700),)},
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signals = all_signals(service)
    assert len(signals) == 1
    assert signals[0]["signal_type"] == "PRICE_MOVE"


def test_duplicate_price_alert_suppressed(temporal):
    service, market = temporal
    first_move = later(market, yes_ask=market.yes_ask + 0.07)
    quiet_pullback = later(first_move, yes_ask=market.yes_ask + 0.045)
    duplicate_churn = later(quiet_pullback, yes_ask=market.yes_ask + 0.096)
    service.replace_inputs([market])
    service.replace_inputs([first_move])
    service.replace_inputs([quiet_pullback])
    service.replace_inputs([duplicate_churn])
    signals = items_of_type(service, SignalType.PRICE_MOVE)
    assert len(signals) == 1
    assert signals[0]["current_value"] == first_move.yes_ask


def test_price_cooldown_preserved(temporal):
    service, market = temporal
    first_move = later(market, yes_ask=market.yes_ask + 0.07)
    quiet_pullback = later(first_move, yes_ask=market.yes_ask + 0.04)
    after_cooldown = later(quiet_pullback, yes_ask=market.yes_ask + 0.095)
    service.replace_inputs([market])
    service.replace_inputs([first_move])
    key = next(iter(service.last_emitted_signals))
    older = timestamp(service.last_emitted_signals[key].detected_at) - timedelta(
        minutes=16
    )
    service.last_emitted_signals[key] = replace(
        service.last_emitted_signals[key],
        detected_at=older.isoformat(),
    )
    service.replace_inputs([quiet_pullback])
    service.replace_inputs([after_cooldown])
    signals = items_of_type(service, SignalType.PRICE_MOVE)
    assert len(signals) == 2


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


def test_spread_beats_liquidity_without_price(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.24,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    current = later(
        baseline,
        yes_bid=0.20,
        executable_depth={"YES": ((0.32, 700),)},
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signals = all_signals(service)
    assert len(signals) == 1
    assert signals[0]["signal_type"] == "SPREAD_MOVE"


def test_only_one_spread_signal_for_mirrored_yes_no_movement(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.24,
        yes_ask=0.32,
        no_bid=0.68,
        no_ask=0.76,
    )
    current = later(
        baseline,
        yes_bid=0.19,
        no_ask=0.80,
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signals = items_of_type(service, SignalType.SPREAD_MOVE)
    assert len(signals) == 1
    assert signals[0]["side"] == "YES"


def test_duplicate_spread_churn_suppressed(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.24, yes_ask=0.32)
    first_widening = later(baseline, yes_bid=0.20, yes_ask=0.32)
    quiet_compression = later(first_widening, yes_bid=0.215, yes_ask=0.32)
    duplicate_widening = later(quiet_compression, yes_bid=0.184, yes_ask=0.32)
    service.replace_inputs([baseline])
    service.replace_inputs([first_widening])
    service.replace_inputs([quiet_compression])
    service.replace_inputs([duplicate_widening])
    signals = items_of_type(service, SignalType.SPREAD_MOVE)
    assert len(signals) == 1
    assert signals[0]["current_value"] == pytest.approx(0.12)


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
    assert signal["significance"] == "MATERIAL"


def test_liquidity_spike_reversal_suppressed(temporal):
    service, market = temporal
    baseline = replace(market, executable_depth={"YES": ((0.32, 85),)})
    spike = later(baseline, executable_depth={"YES": ((0.32, 700),)})
    reversal = later(spike, executable_depth={"YES": ((0.32, 85),)})
    service.replace_inputs([baseline])
    service.replace_inputs([spike])
    service.replace_inputs([reversal])
    signals = items_of_type(service, SignalType.LIQUIDITY_MOVE)
    assert len(signals) == 1
    assert signals[0]["significance"] == "MATERIAL"


def test_one_snapshot_liquidity_spike_is_not_high(temporal):
    service, market = temporal
    baseline = replace(market, executable_depth={"YES": ((0.32, 85),)})
    spike = later(baseline, executable_depth={"YES": ((0.32, 700),)})
    service.replace_inputs([baseline])
    service.replace_inputs([spike])
    signal = items_of_type(service, SignalType.LIQUIDITY_MOVE)[0]
    assert signal["significance"] == "MATERIAL"


def test_persistent_liquidity_change_can_become_high(temporal):
    service, market = temporal
    baseline = replace(market, executable_depth={"YES": ((0.32, 85),)})
    spike = later(baseline, executable_depth={"YES": ((0.32, 700),)})
    persistent = later(spike, executable_depth={"YES": ((0.32, 700),)})
    service.replace_inputs([baseline])
    service.replace_inputs([spike])
    service.replace_inputs([persistent])
    signals = items_of_type(service, SignalType.LIQUIDITY_MOVE)
    assert [signal["significance"] for signal in signals] == ["HIGH", "MATERIAL"]
    assert signals[0]["previous_value"] == 85
    assert signals[0]["current_value"] == 700


def test_duplicate_market_burst_reduced_to_one_surfaced_signal(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.24,
        yes_ask=0.32,
        no_bid=0.68,
        no_ask=0.76,
        executable_depth={
            "YES": ((0.32, 85),),
            "NO": ((0.76, 90),),
        },
    )
    burst = later(
        baseline,
        yes_bid=0.20,
        yes_ask=0.39,
        no_ask=0.81,
        executable_depth={
            "YES": ((0.39, 700),),
            "NO": ((0.81, 650),),
        },
    )
    service.replace_inputs([baseline])
    service.replace_inputs([burst])
    signals = all_signals(service)
    assert len(signals) == 1
    assert signals[0]["signal_type"] == "PRICE_MOVE"


def test_signals_route_and_bounded_history(temporal):
    service, market = temporal
    for step in range(OBSERVATION_HISTORY_LIMIT + 5):
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
