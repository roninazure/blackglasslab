from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from plistlib import load as load_plist
from threading import Thread
from urllib.request import urlopen

import pytest

from parallax.api import server
from parallax.demo import demo_inputs
from parallax.entitlements import Plan
from parallax.models import Side, SignalSignificance, SignalType, timestamp, utcnow
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.service import (
    OBSERVATION_HISTORY_LIMIT,
    PUBLISHABLE_SIGNAL_LIMIT,
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


def tight_context_market(market, **changes):
    defaults = {
        "yes_bid": 0.30,
        "yes_ask": 0.32,
        "event_title": "Villarreal CF vs Real Betis",
        "title": "Both Teams To Score",
        "category": "Soccer",
        "resolution_time": "2099-09-14T23:00:00+00:00",
    }
    defaults.update(changes)
    return replace(market, **defaults)


def test_source_event_metadata_and_url_survive_normalization():
    pmus_market = {
        "id": "pmus-market-1",
        "slug": "villarreal-real-betis-btts",
        "question": "Both Teams To Score",
        "event_id": "pmus-event-1",
        "active": True,
        "closed": False,
        "accepting_orders": True,
        "raw": {
            "title": "",
            "description": "Both teams score market.",
            "category": "Soccer",
            "event": {"title": "Villarreal CF vs Real Betis"},
            "url": "https://example.com/pmus/market",
            "endDate": "2026-09-14T23:00:00+00:00",
            "marketSides": [
                {"long": True, "description": "Yes"},
                {"long": False, "description": "No"},
            ],
            "orderPriceMinTickSize": "0.01",
            "minimumTradeQty": "1",
            "volume24hr": "123",
        },
    }
    pmus = normalize_pmus(
        pmus_market,
        {
            "villarreal-real-betis-btts::YES": {
                "best_bid": "0.30",
                "best_ask": "0.35",
                "bid_size_shares": "100",
                "ask_size_shares": "150",
            },
            "villarreal-real-betis-btts::NO": {
                "best_bid": "0.65",
                "best_ask": "0.70",
                "bid_size_shares": "90",
                "ask_size_shares": "125",
            },
        },
        "2026-09-07T12:00:00+00:00",
    )
    assert pmus.event_title == "Villarreal CF vs Real Betis"
    assert pmus.source_url == "https://example.com/pmus/market"

    kalshi = normalize_kalshi(
        {
            "ticker": "KXLALIGABTTS-26SEP14VILRBB-BTTS",
            "title": "Both Teams To Score",
            "subtitle": "",
            "category": "Soccer",
            "event_ticker": "KXLALIGABTTS-26SEP14VILRBB",
            "yes_sub_title": "Yes",
            "no_sub_title": "No",
            "status": "active",
            "expected_expiration_time": "2026-09-14T23:00:00+00:00",
            "market_type": "binary",
            "notional_value_dollars": "1",
            "price_level_structure": "linear_cent",
            "market_url": "https://example.com/kalshi/market",
        },
        {
            "orderbook_fp": {
                "yes_dollars": [(0.30, 100)],
                "no_dollars": [(0.65, 125)],
            }
        },
        "2026-09-07T12:00:00+00:00",
        event={
            "title": "Villarreal CF vs Real Betis",
            "series_ticker": "KXLALIGABTTS",
        },
    )
    assert kalshi.event_title == "Villarreal CF vs Real Betis"
    assert kalshi.source_url == "https://example.com/kalshi/market"


def test_signal_retail_fields_use_context_and_clean_formatting(temporal):
    service, market = temporal
    baseline = replace(
        market,
        title="Both Teams To Score",
        event_title="Villarreal CF vs Real Betis",
        source_url="https://example.com/market",
        resolution_time="2026-09-14T23:00:00+00:00",
        yes_bid=0.94,
        yes_ask=0.97,
    )
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, seconds=33, yes_bid=0.71, yes_ask=0.74)])
    signal = all_signals(service)[0]
    assert signal["market_title"] == "Both Teams To Score"
    assert signal["event_title"] == "Villarreal CF vs Real Betis"
    assert signal["display_title"] == "Villarreal CF vs Real Betis — Both Teams To Score"
    assert signal["market_url"] == "https://example.com/market"
    assert signal["signal_type"] == "PRICE_MOVE"
    assert signal["signal_label"] == "Price Move"
    assert signal["direction"] == "DOWN"
    assert signal["formatted_previous_value"] == "97¢"
    assert signal["formatted_current_value"] == "74¢"
    assert signal["formatted_change"] == "-23¢"
    assert signal["formatted_window"] == "33 sec"
    assert signal["signal_strength"] == "HIGH"
    assert signal["significance"] == "HIGH"
    assert "confidence" not in signal
    assert signal["resolution_time"] == "2026-09-14T23:00:00+00:00"
    assert signal["resolution_label"] == "Sep 14, 2026"
    assert signal["previous_value"] == pytest.approx(0.97)
    assert signal["current_value"] == pytest.approx(0.74)
    assert signal["absolute_change"] == pytest.approx(0.23)


def test_display_title_avoids_duplicates_and_missing_context_fabrication(
    temporal, tmp_path
):
    service, market = temporal
    titled = replace(
        market,
        title="Villarreal CF vs Real Betis — Both Teams To Score",
        event_title="Villarreal CF vs Real Betis",
        yes_bid=0.30,
        yes_ask=0.32,
    )
    service.replace_inputs([titled])
    service.replace_inputs([later(titled, yes_bid=0.36, yes_ask=0.38)])
    signal = all_signals(service)[0]
    assert signal["display_title"] == "Villarreal CF vs Real Betis — Both Teams To Score"

    service = PlayService(TrackRecord(tmp_path / "missing-context.sqlite"))
    untitled = replace(market, event_title=None, yes_bid=0.30, yes_ask=0.32)
    service.replace_inputs([untitled])
    service.replace_inputs([later(untitled, yes_bid=0.36, yes_ask=0.38)])
    signal = all_signals(service)[0]
    assert signal["event_title"] is None
    assert signal["display_title"] == market.title


def test_first_observation_creates_no_signal(temporal):
    service, market = temporal
    service.replace_inputs([market])
    assert service.signals(Plan.PRO)["items"] == []


def test_material_price_move_without_fair_value_or_order(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.30, yes_ask=0.32)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.37, yes_ask=0.39)])
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


def test_price_move_suppressed_when_previous_book_is_wide(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.05, yes_ask=0.88)
    current = later(baseline, yes_bid=0.94, yes_ask=0.99)
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.PRICE_MOVE) == []


def test_price_move_suppressed_when_current_book_is_wide(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.30, yes_ask=0.32)
    current = later(baseline, yes_bid=0.05, yes_ask=0.88)
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.PRICE_MOVE) == []


def test_price_move_suppressed_for_one_sided_book(temporal):
    service, market = temporal
    missing_bid = replace(market, yes_bid=None, yes_ask=0.32)
    current = later(missing_bid, yes_bid=0.37, yes_ask=0.39)
    service.replace_inputs([missing_bid])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.PRICE_MOVE) == []

    service, market = temporal
    missing_ask = replace(market, yes_bid=0.30, yes_ask=None)
    current = later(missing_ask, yes_bid=0.37, yes_ask=0.39)
    service.replace_inputs([missing_ask])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.PRICE_MOVE) == []


def test_price_beats_spread_and_liquidity_in_same_window(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.27,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    current = later(
        baseline,
        yes_bid=0.37,
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
    baseline = replace(market, yes_bid=0.30, yes_ask=0.32)
    first_move = later(baseline, yes_bid=0.37, yes_ask=0.39)
    quiet_pullback = later(first_move, yes_bid=0.345, yes_ask=0.365)
    duplicate_churn = later(quiet_pullback, yes_bid=0.396, yes_ask=0.416)
    service.replace_inputs([baseline])
    service.replace_inputs([first_move])
    service.replace_inputs([quiet_pullback])
    service.replace_inputs([duplicate_churn])
    signals = items_of_type(service, SignalType.PRICE_MOVE)
    assert len(signals) == 1
    assert signals[0]["current_value"] == first_move.yes_ask


def test_price_cooldown_preserved(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.30, yes_ask=0.32)
    first_move = later(baseline, yes_bid=0.37, yes_ask=0.39)
    quiet_pullback = later(first_move, yes_bid=0.34, yes_ask=0.36)
    after_cooldown = later(quiet_pullback, yes_bid=0.391, yes_ask=0.411)
    service.replace_inputs([baseline])
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
    baseline = replace(market, yes_bid=0.30, yes_ask=0.32)
    service.replace_inputs([baseline])
    service.replace_inputs(
        [
            later(
                baseline,
                yes_ask=0.33,
                yes_bid=0.31,
                executable_depth={"YES": ((0.33, 1050),)},
            )
        ]
    )
    assert service.signals(Plan.PRO)["items"] == []


@pytest.mark.parametrize(
    "bid,ask,direction,label",
    [
        (0.32, 0.33, "COMPRESSED", "Spread Compression"),
        (0.27, 0.32, "WIDENED", "Spread Widening"),
    ],
)
def test_spread_move(temporal, bid, ask, direction, label):
    service, market = temporal
    baseline_bid = 0.27 if direction == "COMPRESSED" else 0.31
    baseline = replace(market, yes_bid=baseline_bid, yes_ask=0.32)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=bid, yes_ask=ask)])
    signals = items_of_type(service, SignalType.SPREAD_MOVE)
    assert len(signals) == 1
    assert direction.lower() in signals[0]["explanation"]
    assert signals[0]["direction"] == direction
    assert signals[0]["signal_label"] == label


def test_spread_beats_liquidity_without_price(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.27,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    current = later(
        baseline,
        yes_bid=0.31,
        executable_depth={"YES": ((0.32, 700),)},
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signals = all_signals(service)
    assert len(signals) == 1
    assert signals[0]["signal_type"] == "SPREAD_MOVE"


def test_spread_move_suppressed_when_endpoint_book_is_wide(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.05, yes_ask=0.88)
    current = later(baseline, yes_bid=0.84, yes_ask=0.88)
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.SPREAD_MOVE) == []

    service, market = temporal
    baseline = replace(market, yes_bid=0.31, yes_ask=0.32)
    current = later(baseline, yes_bid=0.05, yes_ask=0.88)
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.SPREAD_MOVE) == []


def test_only_one_spread_signal_for_mirrored_yes_no_movement(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.27,
        yes_ask=0.32,
        no_bid=0.68,
        no_ask=0.73,
    )
    current = later(
        baseline,
        yes_bid=0.315,
        no_bid=0.72,
        no_ask=0.73,
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signals = items_of_type(service, SignalType.SPREAD_MOVE)
    assert len(signals) == 1
    assert signals[0]["side"] == "YES"


def test_duplicate_spread_churn_suppressed(temporal):
    service, market = temporal
    baseline = replace(market, yes_bid=0.31, yes_ask=0.32)
    first_widening = later(baseline, yes_bid=0.27, yes_ask=0.32)
    quiet_compression = later(first_widening, yes_bid=0.30, yes_ask=0.32)
    duplicate_widening = later(quiet_compression, yes_bid=0.27, yes_ask=0.32)
    service.replace_inputs([baseline])
    service.replace_inputs([first_widening])
    service.replace_inputs([quiet_compression])
    service.replace_inputs([duplicate_widening])
    signals = items_of_type(service, SignalType.SPREAD_MOVE)
    assert len(signals) == 1
    assert signals[0]["current_value"] == pytest.approx(0.05)


def test_liquidity_move(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.30,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    current = later(baseline, executable_depth={"YES": ((0.32, 390),)})
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signal = items_of_type(service, SignalType.LIQUIDITY_MOVE)[0]
    assert signal["previous_value"] == 85
    assert signal["current_value"] == 390
    assert signal["percent_change"] == pytest.approx(358.8235)
    assert signal["significance"] == "MATERIAL"
    assert signal["signal_strength"] == "MATERIAL"
    assert signal["direction"] == "INCREASED"
    assert signal["signal_label"] == "Liquidity Increase"
    assert signal["formatted_previous_value"] == "85 contracts"
    assert signal["formatted_current_value"] == "390 contracts"
    assert signal["formatted_change"] == "+305 contracts"


def test_liquidity_move_suppressed_when_endpoint_book_is_wide(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.05,
        yes_ask=0.88,
        executable_depth={"YES": ((0.88, 85),)},
    )
    current = later(
        baseline,
        yes_bid=0.84,
        yes_ask=0.88,
        executable_depth={"YES": ((0.88, 700),)},
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.LIQUIDITY_MOVE) == []

    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.30,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    current = later(
        baseline,
        yes_bid=0.05,
        yes_ask=0.88,
        executable_depth={"YES": ((0.88, 700),)},
    )
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    assert items_of_type(service, SignalType.LIQUIDITY_MOVE) == []


def test_liquidity_decrease_direction_and_label(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.30,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 390),)},
    )
    current = later(baseline, executable_depth={"YES": ((0.32, 85),)})
    service.replace_inputs([baseline])
    service.replace_inputs([current])
    signal = items_of_type(service, SignalType.LIQUIDITY_MOVE)[0]
    assert signal["direction"] == "DECREASED"
    assert signal["signal_label"] == "Liquidity Decrease"
    assert signal["formatted_change"] == "-305 contracts"


def test_liquidity_spike_reversal_suppressed(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.30,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
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
    baseline = replace(
        market,
        yes_bid=0.30,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    spike = later(baseline, executable_depth={"YES": ((0.32, 700),)})
    service.replace_inputs([baseline])
    service.replace_inputs([spike])
    signal = items_of_type(service, SignalType.LIQUIDITY_MOVE)[0]
    assert signal["significance"] == "MATERIAL"


def test_persistent_liquidity_change_can_become_high(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.30,
        yes_ask=0.32,
        executable_depth={"YES": ((0.32, 85),)},
    )
    spike = later(baseline, executable_depth={"YES": ((0.32, 700),)})
    persistent = later(spike, executable_depth={"YES": ((0.32, 700),)})
    service.replace_inputs([baseline])
    service.replace_inputs([spike])
    service.replace_inputs([persistent])
    signals = items_of_type(service, SignalType.LIQUIDITY_MOVE)
    assert [signal["significance"] for signal in signals] == ["HIGH", "MATERIAL"]
    assert signals[0]["previous_value"] == 85
    assert signals[0]["current_value"] == 700


def test_high_valid_price_move_enters_publishable_queue(temporal):
    service, market = temporal
    baseline = tight_context_market(market)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.43, yes_ask=0.45)])
    payload = service.publishable_signals(Plan.PRO)
    assert payload["total"] == 1
    signal = payload["items"][0]
    assert signal["signal_type"] == "PRICE_MOVE"
    assert signal["publishable"] is True
    assert signal["publishability_reason"] == "HIGH_VALIDATED_SIGNAL"
    assert signal["publishable_until"]
    assert "Not a BUY recommendation" in signal["social_preview"]


def test_material_signal_does_not_enter_publishable_queue(temporal):
    service, market = temporal
    baseline = tight_context_market(market)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.36, yes_ask=0.38)])
    assert service.signals(Plan.PRO)["total"] == 1
    assert service.publishable_signals(Plan.PRO)["items"] == []


def test_high_sports_signal_without_event_context_is_not_publishable(temporal):
    service, market = temporal
    baseline = tight_context_market(market, event_title=None)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.43, yes_ask=0.45)])
    assert service.signals(Plan.PRO)["total"] == 1
    assert service.publishable_signals(Plan.PRO)["items"] == []


def test_stale_high_signal_is_not_publishable(temporal):
    service, market = temporal
    baseline = tight_context_market(market)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.43, yes_ask=0.45)])
    stale_at = utcnow() - timedelta(minutes=6)
    service.signal_history[0] = replace(
        service.signal_history[0],
        detected_at=stale_at.isoformat(),
    )
    assert service.publishable_signals(Plan.PRO)["items"] == []


def test_expired_signal_is_not_publishable(temporal):
    service, market = temporal
    baseline = tight_context_market(market, resolution_time="2000-01-01T00:00:00+00:00")
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.43, yes_ask=0.45)])
    assert service.signals(Plan.PRO)["total"] == 1
    assert service.publishable_signals(Plan.PRO)["items"] == []


def test_publishable_queue_limit_and_ranking(temporal):
    service, market = temporal
    detected_at = utcnow()
    markets = [
        tight_context_market(
            market,
            venue_market_id=f"publishable-{idx}",
            slug=f"publishable-{idx}",
            title=f"Market {idx}",
        )
        for idx in range(12)
    ]
    service.replace_inputs(markets)
    for idx, row in enumerate(markets):
        signal_type = (
            SignalType.PRICE_MOVE
            if idx % 3 == 0
            else SignalType.SPREAD_MOVE
            if idx % 3 == 1
            else SignalType.LIQUIDITY_MOVE
        )
        service.signal_history.append(
            service._signal(
                row,
                signal_type,
                Side.YES,
                0.10 if signal_type != SignalType.LIQUIDITY_MOVE else 100,
                0.25 if signal_type != SignalType.LIQUIDITY_MOVE else 700,
                30,
                SignalSignificance.HIGH,
                "Validated test signal.",
                detected_at,
                percent_change=600 if signal_type == SignalType.LIQUIDITY_MOVE else None,
            )
        )
    payload = service.publishable_signals(Plan.PRO)
    explorer = service.publishable_signals(Plan.EXPLORER)
    assert payload["limit"] == PUBLISHABLE_SIGNAL_LIMIT
    assert len(payload["items"]) == PUBLISHABLE_SIGNAL_LIMIT
    assert len(explorer["items"]) == explorer["limit"] == 3
    types = [item["signal_type"] for item in payload["items"]]
    assert types[:4] == ["PRICE_MOVE"] * 4
    assert types[4:8] == ["SPREAD_MOVE"] * 4
    assert types[8:] == ["LIQUIDITY_MOVE"] * 2


def test_social_preview_is_deterministic_human_readable_and_safe(temporal):
    service, market = temporal
    baseline = tight_context_market(market)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.43, yes_ask=0.45)])
    first = service.publishable_signals(Plan.PRO)["items"][0]["social_preview"]
    second = service.publishable_signals(Plan.PRO)["items"][0]["social_preview"]
    assert first == second
    assert "Polymarket" in first
    assert "Villarreal CF vs Real Betis — Both Teams To Score" in first
    assert "Price Move" in first
    assert "YES: 32¢ -> 45¢" in first
    assert "+13¢ in 1.5 min" in first
    assert "Signal Strength: HIGH" in first
    assert "Not a BUY recommendation" in first
    lowered = first.casefold()
    assert "probability" not in lowered
    assert "fair value" not in lowered
    assert "profit" not in lowered
    assert "payout" not in lowered
    assert "because" not in lowered


def test_publishable_route_health_metric_and_raw_signals_unchanged(temporal):
    service, market = temporal
    baseline = tight_context_market(market)
    service.replace_inputs([baseline])
    service.replace_inputs([later(baseline, yes_bid=0.43, yes_ask=0.45)])
    raw_signal = service.signals(Plan.PRO)["items"][0]
    assert "publishable" not in raw_signal
    assert "social_preview" not in raw_signal
    assert service.health()["metrics"]["publishable_signals"] == 1

    api = server(service, port=0, resolve_plan=lambda headers: Plan.PRO)
    thread = Thread(target=api.serve_forever, daemon=True)
    thread.start()
    try:
        with urlopen(
            f"http://127.0.0.1:{api.server_port}/signals/publishable"
        ) as response:
            payload = json.load(response)
        assert response.status == 200
        assert payload["items"][0]["publishable"] is True
    finally:
        api.shutdown()
        api.server_close()
        thread.join(timeout=2)


def test_launchd_plist_is_user_level_read_only_parallax_service():
    path = Path("deploy/launchd/com.swarmaxis.parallax-intelligence.plist")
    with path.open("rb") as handle:
        plist = load_plist(handle)
    assert plist["Label"] == "com.swarmaxis.parallax-intelligence"
    assert plist["WorkingDirectory"] == "/Users/scottsteele/swarm-runtime/swarm-edge"
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert plist["ProgramArguments"] == [
        "/opt/homebrew/bin/uv",
        "run",
        "python",
        "-m",
        "parallax",
        "serve",
        "--limit",
        "6",
        "--port",
        "8765",
    ]
    assert plist["StandardOutPath"].endswith("parallax-intelligence.log")
    assert plist["StandardErrorPath"].endswith("parallax-intelligence.err.log")
    joined = " ".join(plist["ProgramArguments"])
    assert "sudo" not in joined
    assert "maker" not in joined
    assert "revenue" not in joined


def test_duplicate_market_burst_reduced_to_one_surfaced_signal(temporal):
    service, market = temporal
    baseline = replace(
        market,
        yes_bid=0.27,
        yes_ask=0.32,
        no_bid=0.68,
        no_ask=0.73,
        executable_depth={
            "YES": ((0.32, 85),),
            "NO": ((0.73, 90),),
        },
    )
    burst = later(
        baseline,
        yes_bid=0.37,
        yes_ask=0.39,
        no_bid=0.76,
        no_ask=0.78,
        executable_depth={
            "YES": ((0.39, 700),),
            "NO": ((0.78, 650),),
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
        service.replace_inputs(
            [
                later(
                    market,
                    step * 90,
                    yes_bid=0.30 + step * 0.06,
                    yes_ask=0.32 + step * 0.06,
                )
            ]
        )
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
