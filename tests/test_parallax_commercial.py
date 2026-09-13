from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import timedelta
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from parallax.api import server
from parallax.demo import demo_inputs
from parallax.economics import retail_example
from parallax.engine import qualify
from parallax.entitlements import Feature, Plan, entitlement
from parallax.models import Action, Confidence, Mechanics, PlayType, Side, Venue, utcnow
from parallax.normalization import normalize_kalshi, normalize_pmus, rules_digest
from parallax.service import PlayService
from parallax.track_record import TrackRecord


@pytest.fixture
def sample():
    now = utcnow()
    markets, evidence = demo_inputs(now)
    market = replace(markets[0], demo=False)
    proof = replace(evidence[(market.venue, market.venue_market_id)], demo=False)
    return now, market, proof


def test_normalized_pmus(sample):
    _, market, _ = sample
    assert market.venue == Venue.POLYMARKET
    assert market.yes_ask == 0.32
    assert market.no_ask == 0.70
    assert market.executable_depth["NO"] == ((0.70, 1000),)
    assert market.outcomes["YES"] == "Sample event happens"
    assert market.original_metadata["market"]["description"] == market.resolution_rules


@pytest.mark.parametrize(
    "book",
    [
        {
            "orderbook_fp": {
                "yes_dollars": [["0.35", "20.25"]],
                "no_dollars": [["0.60", "30.50"]],
            }
        },
        {"orderbook": {"yes": [[35, 20.25]], "no": [[60, 30.50]]}},
    ],
)
def test_normalized_kalshi(book):
    market = normalize_kalshi(
        {
            "ticker": "K",
            "status": "active",
            "market_type": "binary",
            "notional_value_dollars": "1.00",
        },
        book,
        utcnow().isoformat(),
    )
    assert market.yes_ask == 0.40 and market.no_ask == 0.65
    assert market.best_ask_size == 30.50
    assert market.original_metadata["book"] == book
    assert market.timestamp_basis.startswith("local REST")


@pytest.mark.parametrize("stake,quantity", [(10, 31), (25, 78), (50, 156), (100, 312)])
def test_retail_stakes(sample, stake, quantity):
    _, market, _ = sample
    row = retail_example(stake, 0.32, 1000, market.mechanics)
    assert row.available
    assert row.contracts_or_shares == quantity
    assert row.amount_spent == pytest.approx(quantity * 0.32)
    assert row.estimated_payout_if_correct == quantity
    assert row.estimated_profit_if_correct == pytest.approx(quantity * 0.68)
    assert row.maximum_loss == row.amount_spent <= stake
    assert row.unspent == pytest.approx(stake - row.amount_spent)


def test_expected_value_is_probability_weighted_not_win_profit(sample):
    _, market, proof = sample
    play = qualify(market, Side.YES, proof, now=utcnow())
    example = play.retail_examples[1]
    independent_ev = (
        play.model_probability * example.net_profit_if_correct
        - (1 - play.model_probability) * example.maximum_loss_including_fees
    )
    assert play.expected_value == pytest.approx(independent_ev)
    assert play.expected_value != pytest.approx(example.net_profit_if_correct)


def test_fractional_quantity():
    row = retail_example(10, 0.32, 100, Mechanics(0.01, 0.01, ((0, 1, 0.01),), 1))
    assert row.contracts_or_shares == 31.25
    assert row.estimated_profit_if_correct == 21.25
    assert row.fees_estimate is None


@pytest.mark.parametrize("stake", [0, -1, float("nan"), float("inf")])
def test_invalid_stake(sample, stake):
    with pytest.raises(ValueError):
        retail_example(stake, 0.32, 1000, sample[1].mechanics)


@pytest.mark.parametrize("price", [0, 1, -1, 1.01, None])
def test_price_boundaries(sample, price):
    assert not retail_example(10, price, 1000, sample[1].mechanics).available


def test_fees_and_slippage():
    mechanics = Mechanics(1, 1, ((0, 1, 0.01),), 1, 0.07, "CEILING")
    row = retail_example(10, 0.32, 100, mechanics, slippage_per_contract=0.01)
    assert row.fees_estimate == 0.48
    assert row.slippage_estimate == 0.31
    assert row.total_cost == 10.71
    assert row.net_profit_if_correct == 20.29
    assert row.maximum_loss_including_fees == 10.71
    # Half-even cents, not floating-point round(): 0.025 -> 0.02.
    row = retail_example(
        0.50, 0.50, 10, replace(mechanics, fee_rate=0.10, fee_rounding="HALF_EVEN")
    )
    assert row.fees_estimate == 0.02


def test_quantity_minimum_tick_and_capacity(sample):
    mech = sample[1].mechanics
    assert not retail_example(10, 0.32, 30, mech).available
    assert not retail_example(0.10, 0.32, 1000, mech).available
    assert not retail_example(10, 0.325, 1000, mech).available
    assert not retail_example(10, 0.32, 1000, replace(mech, payout=10)).available


def test_buy_and_serialization(sample):
    now, market, proof = sample
    play = qualify(market, Side.YES, proof, now=now)
    assert play.suggested_action == Action.BUY
    assert play.confidence_band == Confidence.ELITE
    assert play.edge_points == 14
    assert play.expected_value > 0
    assert play.verdict.failed_gates == ()
    assert json.loads(json.dumps(play.as_dict(), allow_nan=False))["side"] == "YES"


def test_no_side_buy(sample):
    now, market, proof = sample
    proof = replace(proof, fair_probability=0.15)
    play = qualify(market, Side.NO, proof, now=now)
    assert play.suggested_action == Action.BUY
    assert play.model_probability == 0.85
    assert play.current_price == 0.70


def test_high_and_medium_confidence(sample):
    now, market, proof = sample
    high = qualify(market, Side.YES, replace(proof, independent_sources=()), now=now)
    assert (
        high.confidence_band == Confidence.HIGH and high.suggested_action == Action.BUY
    )
    medium = qualify(market, Side.YES, replace(proof, validation_reference=""), now=now)
    assert (
        medium.confidence_band == Confidence.MEDIUM
        and medium.suggested_action == Action.WATCH
    )


@pytest.mark.parametrize(
    "change,code,action",
    [
        ({"book_timestamp": "2020-01-01T00:00:00Z"}, "stale_data", Action.WATCH),
        ({"book_timestamp": "2099-01-01T00:00:00Z"}, "stale_data", Action.WATCH),
        ({"data_timestamp": "2020-01-01T00:00:00Z"}, "stale_data", Action.WATCH),
        ({"status": "CLOSED"}, "market_closed", Action.PASS),
        ({"resolution_rules": ""}, "rules_ambiguous", Action.PASS),
        ({"yes_ask": 0}, "invalid_price", Action.PASS),
        ({"yes_ask": 1}, "invalid_price", Action.PASS),
        ({"yes_bid": 0.10}, "spread", Action.WATCH),
        ({"executable_depth": {"YES": ((0.32, 5),)}}, "liquidity", Action.WATCH),
    ],
)
def test_mandatory_gates(sample, change, code, action):
    now, market, proof = sample
    play = qualify(replace(market, **change), Side.YES, proof, now=now)
    assert code in play.verdict.failed_gates
    assert play.suggested_action == action


@pytest.mark.parametrize(
    "change",
    [
        {"contradictions": ("Official source disagrees",)},
        {"invalidated": True},
        {"rules_digest": "wrong"},
        {"market_id": "wrong"},
        {"venue": Venue.KALSHI},
        {"source": ""},
        {"fair_probability": float("nan")},
        {"demo": True},
    ],
)
def test_bad_evidence_never_buy(sample, change):
    now, market, proof = sample
    assert (
        qualify(market, Side.YES, replace(proof, **change), now=now).suggested_action
        != Action.BUY
    )


def test_no_fair_value_invented(sample):
    now, market, _ = sample
    play = qualify(market, Side.YES, now=now)
    assert play.parallax_fair_value is None and play.expected_value is None
    assert play.suggested_action == Action.WATCH


def test_rule_binding_covers_outcomes(sample):
    now, market, proof = sample
    altered = replace(
        market, outcomes={"YES": "Entirely different outcome", "NO": "Other"}
    )
    assert rules_digest(altered) != proof.rules_digest
    assert qualify(altered, Side.YES, proof, now=now).suggested_action == Action.PASS


def test_unknown_fees(sample):
    now, market, proof = sample
    market = replace(market, mechanics=replace(market.mechanics, fee_rate=None))
    play = qualify(market, Side.YES, proof, now=now)
    assert play.suggested_action == Action.WATCH
    assert "fees_unknown" in play.verdict.failed_gates


def test_immutable_publication_and_deduplication(tmp_path, sample):
    now, market, proof = sample
    store = TrackRecord(tmp_path / "record.sqlite")
    assert store.publish(market, Side.YES, proof, now=now)
    assert not store.publish(
        market, Side.YES, replace(proof, fair_probability=0.60), now=now
    )
    assert store.summary()["publications"][0]["fair_value_at_publication"] == 0.46
    with store.connect() as db:
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE publications SET snapshot='{}'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM publications")


@pytest.mark.parametrize(
    "side,result", [("YES", "WIN"), ("NO", "LOSS"), (None, "VOID")]
)
def test_settlement(tmp_path, sample, side, result):
    now, market, proof = sample
    store = TrackRecord(tmp_path / "record.sqlite")
    store.publish(market, Side.YES, proof, now=now)
    play_id = qualify(market, Side.YES, proof, now=now).id
    args = {
        "venue": market.venue,
        "market_id": market.venue_market_id,
        "winning_side": side,
        "resolution": "VOID" if side is None else "RESOLVED",
        "source_reference": "venue settlement fixture",
        "settled_at": utcnow().isoformat(),
    }
    store.settle(play_id, **args)
    record = store.summary()
    assert record["outcomes"][0]["result"] == result
    assert record["published_plays"] == 1
    assert record["calibration_metrics"] is None
    assert record["win_rate"] == (
        1 if result == "WIN" else 0 if result == "LOSS" else None
    )
    with pytest.raises(sqlite3.IntegrityError):
        store.settle(play_id, **args)


def test_no_demo_or_stale_publications(tmp_path, sample):
    now, market, proof = sample
    store = TrackRecord(tmp_path / "record.sqlite")
    for m, e, date in [
        (replace(market, demo=True), replace(proof, demo=True), now),
        (market, proof, now + timedelta(minutes=2)),
    ]:
        with pytest.raises(ValueError):
            store.publish(m, Side.YES, e, now=date)
    record = store.summary()
    assert record["published_plays"] == 0
    assert (
        record["win_rate"] is None
        and record["roi_at_published_price_before_costs"] is None
    )


@pytest.fixture
def service(tmp_path):
    result = PlayService(TrackRecord(tmp_path / "record.sqlite"))
    markets, evidence = demo_inputs()
    result.replace_inputs(markets + markets, evidence, mode="demo")
    return result


def test_multi_venue_filter_and_entitlements(service):
    assert len(service.plays(Plan.PRO)["items"]) == 4
    assert len(service.plays(Plan.PRO, venue="KALSHI")["items"]) == 2
    assert len(service.plays(Plan.PRO, action="BUY")["items"]) == 1
    assert len(service.plays(Plan.PRO, resolution_horizon=1)["items"]) == 0
    assert len(service.plays(Plan.PRO, minimum_edge=12)["items"]) == 1
    with pytest.raises(PermissionError):
        service.plays(Plan.EXPLORER, action="BUY")
    with pytest.raises(ValueError):
        service.plays(Plan.PRO, minimum_edge="NaN")
    with pytest.raises(ValueError):
        service.plays(Plan.PRO, plan_override="pro")
    assert not entitlement(Plan.EDGE).permits(Feature.DETAILS)
    assert entitlement(Plan.API).permits(Feature.API_ACCESS)
    assert service.store.summary()["published_plays"] == 0


def test_service_publishes_before_returning_buy(tmp_path, sample):
    _, market, proof = sample
    service = PlayService(TrackRecord(tmp_path / "record.sqlite"))
    service.replace_inputs([market], {(market.venue, market.venue_market_id): proof})
    assert service.plays(Plan.PRO)["items"][0]["suggested_action"] == "BUY"
    assert service.store.summary()["published_plays"] == 1
    service.plays(Plan.PRO)
    assert service.store.summary()["published_plays"] == 1


def test_http_routes_and_access(service):
    api = server(service, port=0)
    thread = Thread(target=api.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{api.server_port}"
    try:
        for route in ("/plays", "/markets", "/venues", "/track-record", "/health"):
            with urlopen(base + route) as response:
                assert response.status == 200
                assert response.headers["Cache-Control"] == "no-store"
                assert json.load(response) is not None
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/plays?plan=pro")
        assert error.value.code == 400
        play_id = service.plays(Plan.PRO)["items"][0]["id"]
        with pytest.raises(HTTPError) as error:
            urlopen(base + f"/plays/{play_id}")
        assert error.value.code == 403
    finally:
        api.shutdown()
        api.server_close()
        thread.join(timeout=2)


def test_streamlit_demo(tmp_path, monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("PARALLAX_MODE", "demo")
    monkeypatch.setenv("PARALLAX_PRODUCT_DB", str(tmp_path / "ui.sqlite"))
    app = AppTest.from_file("dashboard/pages/1_PARALLAX.py").run(timeout=20)
    assert not app.exception
    assert app.title[0].value == "PARALLAX"
    assert "SYNTHETIC DEMO" in app.warning[0].value
    assert any("No published actionable" in item.value for item in app.info)


def test_pmus_float_complement_and_selection_title(sample):
    now, market, _ = sample
    raw = dict(
        market.original_metadata["market"],
        title="Specific selection",
        orderPriceMinTickSize=0.001,
    )
    row = {
        "raw": raw,
        "id": "M",
        "slug": "m",
        "question": "Which outcome wins?",
        "event_id": "E",
        "active": True,
        "closed": False,
        "accepting_orders": True,
    }
    book = {
        "m::YES": {
            "best_bid": 0.843,
            "best_ask": 0.844,
            "bid_size_shares": 1000,
            "ask_size_shares": 1000,
        },
        "m::NO": {
            "best_bid": 1 - 0.844,
            "best_ask": 1 - 0.843,
            "bid_size_shares": 1000,
            "ask_size_shares": 1000,
        },
    }
    normalized = normalize_pmus(row, book, now.isoformat())
    assert normalized.no_ask == 0.157
    assert "Specific selection" in normalized.title
    assert retail_example(10, normalized.no_ask, 1000, normalized.mechanics).available


def test_expired_fee_schedule_not_shown_in_retail_math(sample):
    now, market, proof = sample
    market = replace(
        market,
        mechanics=replace(
            market.mechanics, fee_valid_until=(now - timedelta(seconds=1)).isoformat()
        ),
    )
    play = qualify(market, Side.YES, proof, now=now)
    assert play.suggested_action == Action.WATCH
    assert all(
        e.fees_estimate is None and e.net_profit_if_correct is None
        for e in play.retail_examples
    )


def test_cached_live_buy_expires(service, sample, monkeypatch):
    now, market, proof = sample
    service.replace_inputs([market], {(market.venue, market.venue_market_id): proof})
    assert service.plays(Plan.PRO, action="BUY")["items"]
    monkeypatch.setattr("parallax.service.utcnow", lambda: now + timedelta(seconds=61))
    assert service.plays(Plan.PRO, action="BUY")["items"] == []


def test_publication_failure_cannot_leak_buy(service, sample, monkeypatch):
    _, market, proof = sample
    service.replace_inputs([market], {(market.venue, market.venue_market_id): proof})

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("Disk unavailable")

    monkeypatch.setattr(service.store, "publish", broken)
    assert service.plays(Plan.PRO, action="BUY")["items"] == []
    assert service.failures > 0


def test_unmatched_settlement_rejected(tmp_path, sample):
    now, market, proof = sample
    store = TrackRecord(tmp_path / "record.sqlite")
    store.publish(market, Side.YES, proof, now=now)
    with pytest.raises(ValueError, match="does not match"):
        store.settle(
            qualify(market, Side.YES, proof, now=now).id,
            venue="KALSHI",
            market_id=market.venue_market_id,
            winning_side="YES",
            resolution="RESOLVED",
            source_reference="fixture",
            settled_at=utcnow().isoformat(),
        )


def test_demo_cannot_mix_with_live(service, sample):
    with pytest.raises(ValueError, match="never be mixed"):
        service.replace_inputs([sample[1]], mode="demo")


def test_value_and_reprice_taxonomy(sample):
    now, market, proof = sample
    value = qualify(
        market, Side.YES, replace(proof, play_type=PlayType.PARALLAX_VALUE), now=now
    )
    reprice = qualify(market, Side.YES, replace(proof, new_information=True), now=now)
    assert (
        value.play_type == PlayType.PARALLAX_VALUE
        and value.suggested_action == Action.BUY
    )
    assert (
        reprice.play_type == PlayType.PARALLAX_REPRICE
        and reprice.suggested_action == Action.BUY
    )
