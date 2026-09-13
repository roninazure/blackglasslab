"""Read-only, explicitly expiring venue fee review. No promotions or rebates."""

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

from .models import NormalizedMarket, Venue, timestamp
from .normalization import number

PMUS_SCHEDULE = "https://docs.polymarket.us/fees"
KALSHI_SCHEDULE = "https://kalshi.com/docs/kalshi-fee-schedule.pdf"
KALSHI_ROUNDING = "https://docs.kalshi.com/getting_started/fee_rounding"
# Fixed review window: refreshing books must not renew a static policy review.
REVIEWED_AT = "2026-09-12T20:40:00+00:00"
REVIEW_EXPIRES = "2026-10-12T20:40:00+00:00"


def attach_fees(market: NormalizedMarket, now: datetime, *, event=None, series=None):
    source = PMUS_SCHEDULE if market.venue == Venue.POLYMARKET else KALSHI_SCHEDULE
    proof = {
        "schedule": source,
        "reviewed_at": REVIEWED_AT,
        "review_expires": REVIEW_EXPIRES,
        "event": event,
        "series": series,
        "scope": "taker entry, hold to binary settlement; no rebates or funding charges",
        "verification_timestamp": REVIEWED_AT,
    }
    mechanics = replace(
        market.mechanics,
        fee_rate=None,
        fee_source=source,
        fee_status="UNVERIFIED",
        fee_observed_at=now.isoformat(),
        fee_valid_until=None,
        fee_buffer_per_contract=0,
    )
    reviewed, expires = timestamp(REVIEWED_AT), timestamp(REVIEW_EXPIRES)
    assert reviewed and expires
    reason = "Fee policy review expired or not yet effective"
    if reviewed <= now < expires:
        if market.venue == Venue.POLYMARKET:
            mechanics = replace(
                mechanics,
                fee_rate=0.05,
                fee_rounding="HALF_EVEN",
                fee_status="VERIFIED_UPPER_BOUND",
                fee_valid_until=expires.isoformat(),
            )
            proof["effective_at"] = "2026-04-03T15:00:00-04:00"
            proof["maker_rebate_coefficient"] = -0.0125
            reason = "Exchange-wide taker theta 0.05; half-even cumulative order cap; maker rebate recorded separately"
        elif (
            event
            and series
            and event.get("event_ticker") == market.event
            and market.event
            and series.get("ticker") == "KXMLBGAME"
            and event.get("series_ticker") == series.get("ticker")
        ):
            kind = event.get("fee_type_override")
            if kind is None:
                kind = series.get("fee_type")
            multiplier = event.get("fee_multiplier_override")
            if multiplier is None:
                multiplier = series.get("fee_multiplier")
            multiplier = None if isinstance(multiplier, bool) else number(multiplier)
            reason = "Unsupported fee type or missing/invalid multiplier"
            raw = market.original_metadata.get("market", {})
            observed = timestamp(market.data_timestamp)
            bound = (
                raw.get("ticker") == market.venue_market_id
                and raw.get("event_ticker") == market.event
                and observed is not None
                and 0 <= (now - observed).total_seconds() < 60
            )
            unsupported = [
                key
                for key in raw
                if key.startswith("fee_")
                and key != "fee_waiver_expiration_time"
                and raw[key] is not None
            ]
            waiver_raw = raw.get("fee_waiver_expiration_time")
            waiver_end = timestamp(waiver_raw)
            if not bound:
                reason = "Market fee metadata mismatched or stale"
            elif unsupported or (waiver_raw and waiver_end is None):
                reason = (
                    "Unrecognized market-specific fee override; cannot verify costs"
                )
            elif (
                kind in {"quadratic", "quadratic_with_maker_fees"}
                and multiplier is not None
                and multiplier >= 0
            ):
                rate = float(Decimal("0.07") * Decimal(str(multiplier)))
                assert observed is not None
                valid_until = min(expires, observed + timedelta(seconds=60))
                if waiver_end and now < waiver_end:
                    rate = 0.0
                    valid_until = min(valid_until, waiver_end)
                    proof["zero_fee_evidence"] = "Explicit unexpired market fee waiver"
                elif rate == 0:
                    proof["zero_fee_evidence"] = (
                        "Explicit zero multiplier in bound event/series metadata"
                    )
                mechanics = replace(
                    mechanics,
                    fee_rate=rate,
                    fee_rounding="KALSHI_BALANCE",
                    fee_status="VERIFIED_UPPER_BOUND",
                    fee_valid_until=valid_until.isoformat(),
                    # The current verified KXMLBGAME schedule is estimated
                    # directly; no unverified per-contract uncertainty buffer.
                    fee_buffer_per_contract=0,
                    fee_balance_precision=0.01,
                )
                proof["rounding_source"] = KALSHI_ROUNDING
                proof["effective_multiplier"] = multiplier
                proof["verified_series"] = "KXMLBGAME"
                proof["account_precision"] = (
                    "Unknown account: conservative $0.01 alignment (direct members use $0.0001)"
                )
                proof["estimate_kind"] = (
                    "Worst-case bound, not expected fees: unknown account precision and fill allocation; no rounding rebates assumed"
                )
                reason = "Series/event taker schedule and market waiver checked; six-decimal model rounding plus worst-case fragmented-fill balance rounding"
        else:
            reason = "Market-bound event and series fee metadata unavailable"
    proof["reason"] = reason
    proof["status"] = mechanics.fee_status
    return replace(
        market,
        mechanics=mechanics,
        original_metadata={**market.original_metadata, "fee_provenance": proof},
    )
