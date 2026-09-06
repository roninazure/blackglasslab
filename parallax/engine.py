from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from .economics import retail_example
from .models import (
    Action,
    Confidence,
    Evidence,
    NormalizedMarket,
    ParallaxPlay,
    PlayType,
    Side,
    Verdict,
    timestamp,
    utcnow,
)
from .normalization import rules_digest

MAX_AGE_SECONDS = 60
MIN_EDGE = 0.05
MAX_SPREAD = 0.05


def qualify(
    market: NormalizedMarket,
    side: Side,
    evidence: Evidence | None = None,
    *,
    now: datetime | None = None,
) -> ParallaxPlay:
    now = now or utcnow()
    side = Side(side)
    price = market.yes_ask if side == Side.YES else market.no_ask
    bid = market.yes_bid if side == Side.YES else market.no_bid
    levels = market.executable_depth.get(side, ())
    size = sum(q for p, q in levels if p == price and math.isfinite(q) and q > 0)
    book_at, data_at = (
        timestamp(market.book_timestamp),
        timestamp(market.data_timestamp),
    )
    fresh = all(
        t is not None and 0 <= (now - t).total_seconds() <= MAX_AGE_SECONDS
        for t in (book_at, data_at)
    )
    resolution = timestamp(market.resolution_time)
    evidence_time = timestamp(evidence.observed_at) if evidence else None
    evidence_end = timestamp(evidence.valid_until) if evidence else None
    evidence_bound = bool(
        evidence
        and evidence.venue == market.venue
        and evidence.market_id == market.venue_market_id
        and evidence.demo == market.demo
        and evidence.source.strip()
        and evidence.model_version.strip()
        and evidence.rationale.strip()
        and math.isfinite(evidence.fair_probability)
        and 0 < evidence.fair_probability < 1
    )
    evidence_fresh = bool(
        evidence_time and evidence_end and evidence_time <= now < evidence_end
    )
    reviewed_rules = bool(
        market.resolution_rules.strip()
        and evidence_bound
        and evidence
        and evidence.rules_digest == rules_digest(market)
        and evidence.review_reference.strip()
    )
    validated = bool(
        evidence_bound and evidence and evidence.validation_reference.strip()
    )
    corroborated = bool(
        evidence
        and len({s.strip() for s in evidence.independent_sources if s.strip()}) >= 2
    )
    clear = bool(evidence and not evidence.contradictions and not evidence.invalidated)
    evidence_checks = (
        evidence_bound,
        evidence_fresh,
        reviewed_rules,
        validated,
        corroborated,
        clear,
    )
    score = round(100 * sum(evidence_checks) / len(evidence_checks), 1)
    confidence = (
        Confidence.ELITE
        if all(evidence_checks)
        else Confidence.HIGH
        if all((evidence_bound, evidence_fresh, reviewed_rules, validated, clear))
        else Confidence.MEDIUM
        if evidence_bound and evidence_fresh and clear
        else Confidence.PASS
    )
    fair = (
        (
            evidence.fair_probability
            if side == Side.YES
            else 1 - evidence.fair_probability
        )
        if evidence_bound and evidence_fresh and evidence
        else None
    )
    valid_quote = bool(
        price is not None
        and bid is not None
        and math.isfinite(price)
        and math.isfinite(bid)
        and 0 < bid < price < 1
        and size > 0
    )
    edge = (
        fair - price if fair is not None and valid_quote and price is not None else None
    )
    fee_until = timestamp(market.mechanics.fee_valid_until)
    fees_known = bool(
        market.mechanics.fee_rate is not None
        and market.mechanics.fee_source != "Unknown"
        and fee_until
        and now < fee_until
    )
    mechanics = (
        market.mechanics if fees_known else replace(market.mechanics, fee_rate=None)
    )
    examples = tuple(
        retail_example(stake, price, size, mechanics) for stake in (10, 25, 50, 100)
    )
    small = examples[
        1
    ]  # Qualification requires the $25 scenario, not a token-sized quote.
    ev = (
        fair * small.estimated_payout_if_correct - small.total_cost
        if fair is not None
        and small.available
        and small.total_cost is not None
        and fees_known
        else None
    )
    expected_return = (
        ev / small.total_cost if ev is not None and small.total_cost else None
    )
    gates = {
        "market_closed": (market.status == "OPEN", "This market is not open."),
        "invalid_price": (valid_quote, "A valid two-sided entry price is unavailable."),
        "stale_data": (fresh, "The price needs a fresh check."),
        "resolution_unknown": (
            bool(resolution and resolution > now),
            "The resolution date needs verification or has passed.",
        ),
        "rules_ambiguous": (
            reviewed_rules,
            "The exact outcome and settlement rules need review.",
        ),
        "evidence_missing": (
            evidence_bound and evidence_fresh,
            "A current, sourced PARALLAX value is not available.",
        ),
        "confidence": (
            confidence in (Confidence.ELITE, Confidence.HIGH),
            "Evidence is not strong enough for a BUY.",
        ),
        "contradiction": (
            not evidence or clear,
            "Contradictory evidence or an invalidation blocks this play.",
        ),
        "liquidity": (
            small.available,
            "There are not enough verified contracts for a $25 example.",
        ),
        "spread": (
            bool(
                valid_quote
                and price is not None
                and bid is not None
                and price - bid <= MAX_SPREAD + 1e-9
            ),
            "The gap between buy and sell prices is too wide.",
        ),
        "fees_unknown": (fees_known, "Current venue fees need verification."),
        "edge": (
            edge is not None and edge >= MIN_EDGE - 1e-9,
            "The estimated pricing edge is below five points or unknown.",
        ),
        "risk_reward": (
            bool(expected_return is not None and expected_return >= 0.05),
            "Expected return after estimated costs must be at least 5%.",
        ),
    }
    failed = tuple(k for k, (ok, _) in gates.items() if not ok)
    hard = {"market_closed", "invalid_price", "rules_ambiguous", "contradiction"}
    if edge is not None and edge <= 0:
        hard.add("edge")
    if resolution and resolution <= now:
        hard.add("resolution_unknown")
    action = (
        Action.PASS
        if hard.intersection(failed)
        else Action.WATCH
        if failed
        else Action.BUY
    )
    if action == Action.PASS:
        confidence = Confidence.PASS
    reasons = tuple(message for ok, message in gates.values() if not ok)
    primary = (
        reasons[0]
        if reasons
        else "The estimated value clears the price, evidence and retail cost checks."
    )
    supporting: tuple[str, ...] = (
        (evidence.rationale,) if evidence_bound and evidence else ()
    )
    if action == Action.BUY:
        assert edge is not None
        supporting += (
            f"PARALLAX estimates a {edge * 100:.1f}-point pricing edge.",
            "The displayed price supports a $25 example.",
        )
    risks: tuple[str, ...] = (
        "You can lose the full amount spent, plus fees.",
        "The model can be wrong; news and prices can change.",
    )
    if evidence:
        risks += evidence.contradictions
    invalidations: tuple[str, ...] = (
        "Price, available contracts, market status or settlement rules change.",
        "The source changes or its evidence expires.",
        "The price check is more than 60 seconds old.",
    )
    if fair is not None:
        invalidations += (
            f"Entry price rises above ${max(0, fair - MIN_EDGE):.4f}; cost checks can invalidate it sooner.",
        )
    verdict = Verdict(action, primary, supporting, risks, invalidations, failed)
    kind = (
        PlayType.PARALLAX_AVOID
        if action == Action.PASS
        else PlayType.PARALLAX_REPRICE
        if evidence and evidence.new_information
        else PlayType.PARALLAX_VALUE
        if evidence and evidence.play_type == PlayType.PARALLAX_VALUE
        else PlayType.PARALLAX_EDGE
        if edge is not None and edge >= MIN_EDGE
        else PlayType.PARALLAX_VALUE
    )
    expiry = min(
        [now + timedelta(seconds=MAX_AGE_SECONDS)]
        + [
            t
            for t in (
                book_at + timedelta(seconds=MAX_AGE_SECONDS) if book_at else None,
                data_at + timedelta(seconds=MAX_AGE_SECONDS) if data_at else None,
                evidence_end,
                resolution,
            )
            if t is not None
        ]
    )
    identity = (
        f"parallax:v1:{market.demo}:{market.venue}:{market.venue_market_id}:{side}"
    )
    return ParallaxPlay(
        str(uuid5(NAMESPACE_URL, identity)),
        market.data_timestamp,
        now.isoformat(),
        expiry.isoformat(),
        market.venue,
        market.venue_market_id,
        market.title,
        market.source_url,
        f"{market.venue}:{market.slug}",
        side,
        market.outcomes.get(side, side),
        kind,
        price,
        fair,
        None if edge is None else round(edge * 100, 8),
        score,
        confidence,
        "Deterministic evidence qualification (six checks); not win probability",
        min(100, round(size * price / 25 * 100, 1)) if valid_quote and price else 0,
        "FRESH" if fresh else "STALE_OR_UNKNOWN",
        action,
        primary,
        supporting,
        risks,
        market.resolution_time,
        (resolution - now).total_seconds() if resolution else None,
        examples,
        price,
        size,
        round(min(100, size * price), 8) if valid_quote and price else 0,
        small.fees_estimate if fees_known else None,
        small.slippage_estimate if small.available else None,
        ev,
        expected_return,
        price,
        fair,
        invalidations,
        reasons,
        verdict,
        "DEMO" if market.demo else "CURRENT" if fresh else "STALE",
        market.demo,
        evidence,
    )
