"""Synthetic regression inputs for the read-only Part 2 census and fee reviews."""

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from parallax.census import run_census
from parallax.demo import demo_inputs
from parallax.economics import kalshi_fill_fees, pmus_fill_fees, retail_example
from parallax.engine import qualify
from parallax.fair_value import assess_value
from parallax.fees import REVIEW_EXPIRES, REVIEWED_AT, attach_fees
from parallax.models import Side, timestamp
from parallax.track_record import TrackRecord


@pytest.fixture
def inputs():
    now = timestamp(REVIEWED_AT) + timedelta(minutes=1)
    markets, _ = demo_inputs(now)
    markets = [replace(m, demo=False) for m in markets]
    event = {"event_ticker": markets[1].event, "series_ticker": "TEST"}
    series = {"ticker": "TEST", "fee_type": "quadratic", "fee_multiplier": 1}
    return now, markets, event, series


def test_pmus_current_theta_and_bankers_rounding(inputs):
    now, markets, _, _ = inputs
    market = attach_fees(markets[0], now)
    assert market.mechanics.fee_rate == 0.06
    assert pmus_fill_fees([(1000, 0.50)]) == (Decimal("15.00"),)
    assert pmus_fill_fees([(1000, 0.10)]) == (Decimal("5.40"),)
    # Exact .045 and .075 tie cases with current theta.
    assert pmus_fill_fees([(3, 0.50)]) == (Decimal(".04"),)
    assert pmus_fill_fees([(5, 0.50)]) == (Decimal(".08"),)
    example = retail_example(2.5, 0.50, 100, market.mechanics)
    assert example.fees_estimate == 0.08
    assert "April" not in market.original_metadata["fee_provenance"]["reason"]


def test_pmus_multiple_fills_cumulative_cap():
    assert pmus_fill_fees([(1, 0.50), (1, 0.50), (1, 0.50)]) == (
        Decimal(".02"),
        Decimal(".01"),
        Decimal(".01"),
    )
    # Capping only reduces charges; it never increases a rounded-down fill fee.
    assert pmus_fill_fees([(0.1, 0.50)] * 10) == (Decimal(0),) * 10


@pytest.mark.parametrize(
    "multiplier,override,expected",
    [(1, None, 0.07), (2, None, 0.14), (2, 0.5, 0.035), (1, 0, 0)],
)
def test_kalshi_multiplier_precedence(inputs, multiplier, override, expected):
    now, markets, event, series = inputs
    series["fee_multiplier"] = multiplier
    event["fee_multiplier_override"] = override
    result = attach_fees(markets[1], now, event=event, series=series)
    assert result.mechanics.fee_rate == expected
    if expected == 0:
        assert result.original_metadata["fee_provenance"]["zero_fee_evidence"]


@pytest.mark.parametrize(
    "multiplier", [None, "", -1, float("nan"), float("inf"), False]
)
def test_unknown_or_invalid_fees_are_not_zero(inputs, multiplier):
    now, markets, event, series = inputs
    series["fee_multiplier"] = multiplier
    result = attach_fees(markets[1], now, event=event, series=series)
    assert result.mechanics.fee_rate is None
    assert result.mechanics.fee_status == "UNVERIFIED"
    play = qualify(result, Side.YES, now=now)
    assert "fees_unknown" in play.verdict.failed_gates
    assert play.fees_estimate is None


def test_event_type_override_does_not_fall_back(inputs):
    now, markets, event, series = inputs
    event["fee_type_override"] = "unsupported"
    assert (
        attach_fees(markets[1], now, event=event, series=series).mechanics.fee_rate
        is None
    )


def test_missing_or_mismatched_metadata(inputs):
    now, markets, event, series = inputs
    assert attach_fees(markets[1], now).mechanics.fee_rate is None
    event["event_ticker"] = "WRONG"
    assert (
        attach_fees(markets[1], now, event=event, series=series).mechanics.fee_rate
        is None
    )


def test_explicit_market_waiver_and_unrecognized_override(inputs):
    now, markets, event, series = inputs
    market = markets[1]
    waiver_end = now + timedelta(seconds=20)
    raw = {
        **market.original_metadata["market"],
        "fee_waiver_expiration_time": waiver_end.isoformat(),
    }
    market = replace(
        market, original_metadata={**market.original_metadata, "market": raw}
    )
    result = attach_fees(market, now, event=event, series=series)
    assert result.mechanics.fee_rate == 0
    assert timestamp(result.mechanics.fee_valid_until) == waiver_end
    assert (
        attach_fees(market, waiver_end, event=event, series=series).mechanics.fee_rate
        == 0.07
    )
    raw["fee_multiplier_override"] = (
        9  # Undocumented market schema: reject, never ignore.
    )
    assert (
        attach_fees(market, now, event=event, series=series).mechanics.fee_rate is None
    )


def test_static_policy_review_never_renews_on_refresh(inputs):
    now, markets, event, series = inputs
    for date in (
        timestamp(REVIEWED_AT) - timedelta(seconds=1),
        timestamp(REVIEW_EXPIRES),
    ):
        assert attach_fees(markets[0], date).mechanics.fee_rate is None
    assert (
        attach_fees(
            markets[1], now + timedelta(seconds=60), event=event, series=series
        ).mechanics.fee_rate
        is None
    )


def test_kalshi_six_decimal_rounding_and_account_precision():
    # Official example: cost .055, exact fee .00363825 => fee .005 for cent alignment.
    assert kalshi_fill_fees([(1, 0.055)], 0.07, balance_precision=0.01) == (
        Decimal(".005"),
    )
    assert kalshi_fill_fees([(1, 0.055)], 0.07) == (Decimal(".0037"),)
    # Distinguishes ceil_6dp from old ceil_4dp when cost has fractional cents.
    assert kalshi_fill_fees([(0.01, 0.1234)], 0.07) == (Decimal(".000166"),)
    charges = kalshi_fill_fees([(1, 0.50)] * 100, 0.07, balance_precision=0.01)
    assert sum(charges) == Decimal("1.75")
    assert all(charge >= 0 for charge in charges)


def test_kalshi_upper_bound_covers_fragmented_fills(inputs):
    now, markets, event, series = inputs
    market = attach_fees(markets[1], now, event=event, series=series)
    example = retail_example(10, 0.42, 1000, market.mechanics)
    fills = [(0.01, 0.42)] * (int(example.contracts_or_shares) * 100)
    actual = sum(kalshi_fill_fees(fills, 0.07, balance_precision=0.01))
    assert Decimal(str(example.fees_estimate)) >= actual
    assert market.mechanics.fee_buffer_per_contract == 1.0001
    assert (
        retail_example(10, 0.42, 1000, market.mechanics).maximum_loss_including_fees
        > example.amount_spent
    )


def test_zero_coefficient_does_not_hide_balance_rounding():
    assert kalshi_fill_fees([(1, 0.1234)], 0, balance_precision=0.01) == (
        Decimal(".0066"),
    )


def test_exact_reference_is_not_probability(inputs):
    now, markets, _, _ = inputs
    first = markets[0]
    other = replace(first, venue=markets[1].venue, venue_market_id="DIFFERENT")
    result = assess_value(first, [first, other], now)
    assert result["status"] == "UNVALIDATED_REFERENCE"
    assert result["fair_value"] is None
    assert qualify(first, Side.YES, now=now).parallax_fair_value is None
    changed = replace(
        other, resolution_rules=other.resolution_rules + " Different cancellation rule."
    )
    assert not assess_value(first, [first, changed], now)["references"]
    assert not assess_value(first, [first], now)["references"]


def test_census_never_publishes_or_fabricates_value(inputs, monkeypatch):
    now, markets, event, series = inputs
    markets = [attach_fees(m, now, event=event, series=series) for m in markets]
    markets = [
        replace(
            m,
            original_metadata={
                **m.original_metadata,
                "fair_value_provenance": assess_value(m, markets, now),
            },
        )
        for m in markets
    ]

    def forbidden(*args, **kwargs):
        pytest.fail("Census must not publish")

    monkeypatch.setattr(TrackRecord, "publish", forbidden)
    monkeypatch.setattr("parallax.census.collect_markets", lambda limit: (markets, {}))
    monkeypatch.setattr("parallax.census.utcnow", lambda: now)
    result = run_census(2)
    assert result["live_orders"] == 0 and result["published"] is False
    assert len(result["candidates"]) == 4
    assert all(
        row["fair_value"] is None and row["verdict"] != "BUY"
        for row in result["candidates"]
    )
