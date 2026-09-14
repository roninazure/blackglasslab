#!/usr/bin/env python3
"""Bounded public-read PAPER_ONLY maker spread/rebate observations."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from maker_spread_economics.live import (  # noqa: E402
    ReadOnlyPublicClient,
    discover_markets,
    fetch_book_observation,
    fetch_public_trades,
)
from maker_spread_economics.fill import (  # noqa: E402
    HypotheticalMakerQuote,
    MakerFillFollowup,
    combine_fill_evidence,
    conservative_fill_probability,
    hypothetical_quotes,
    infer_side_fill_evidence,
)
from maker_spread_economics.model import (  # noqa: E402
    MakerFeeMetadata,
    MakerValidationConfig,
    evaluate_maker_quote,
)
from maker_spread_economics.paper import (  # noqa: E402
    initialize_paper_db,
    record_followup,
    record_hypothetical_quote,
    record_prediction,
)


DISCOVERY_REFRESH_SECONDS = 60.0


class CandidateRoundRobin:
    def __init__(self, candidates: list[dict]) -> None:
        self._candidates = list(candidates)
        self._cursor = 0
        self.generation = 1

    def __len__(self) -> int:
        return len(self._candidates)

    def refresh(self, candidates: list[dict]) -> None:
        next_market_id = None
        if self._candidates:
            next_market_id = str(self._candidates[self._cursor]["market"].get("conditionId") or "")
        self._candidates = list(candidates)
        ids = self.market_ids
        if next_market_id in ids:
            self._cursor = ids.index(next_market_id)
        elif self._candidates:
            self._cursor %= len(self._candidates)
        else:
            self._cursor = 0
        self.generation += 1

    @property
    def market_ids(self) -> list[str]:
        return [str(candidate["market"].get("conditionId") or "") for candidate in self._candidates]

    def next(self) -> dict:
        if not self._candidates:
            raise LookupError("no maker candidates available")
        candidate = self._candidates[self._cursor]
        self._cursor = (self._cursor + 1) % len(self._candidates)
        return candidate


def observe_next_candidate(
    client: ReadOnlyPublicClient,
    candidates: CandidateRoundRobin,
    *,
    fee_cache: dict[str, MakerFeeMetadata],
    config: MakerValidationConfig,
    quote_observation_seconds: float,
) -> tuple[
    dict | None,
    object | None,
    tuple[HypotheticalMakerQuote, HypotheticalMakerQuote] | None,
    MakerFillFollowup | None,
    float | None,
    dict[str, MakerFeeMetadata],
    list[dict[str, str]],
]:
    errors = []
    for _ in range(len(candidates)):
        candidate = candidates.next()
        market_id = str(candidate["market"].get("conditionId") or "")
        try:
            signal_book, fee_cache = fetch_book_observation(client, candidate, fee_cache=fee_cache)
            time.sleep(config.latency_seconds)
            activation_book, fee_cache = fetch_book_observation(client, candidate, fee_cache=fee_cache)
            signal = signal_book.quote
            activation = activation_book.quote
            quotes = hypothetical_quotes(
                market_id=signal.market_id,
                token_id=signal.token_id,
                bid=signal.best_bid,
                ask=signal.best_ask,
                size_shares=config.target_shares,
                signaled_at_utc=signal.observed_at_utc,
                eligible_from_utc=activation.observed_at_utc,
                bid_depth_shares=signal.bid_size_shares,
                ask_depth_shares=signal.ask_size_shares,
            )
            time.sleep(quote_observation_seconds)
            post_book, fee_cache = fetch_book_observation(client, candidate, fee_cache=fee_cache)
            post_quote = post_book.quote
            evaluation = evaluate_maker_quote(
                signal_quote=signal,
                activation_quote=activation,
                post_quote=post_quote,
                fee_metadata=fee_cache[market_id],
                config=config,
            )
            evidence_error = None
            try:
                trades = fetch_public_trades(
                    client,
                    market_id=market_id,
                    token_id=activation.token_id,
                    after_utc=activation.observed_at_utc,
                )
                followup_available = evaluation.quote_survived_latency
                if not followup_available:
                    evidence_error = "hypothetical quote was stale before activation"
            except Exception as exc:
                trades = ()
                followup_available = False
                evidence_error = f"public trade follow-up unavailable: {type(exc).__name__}: {exc}"
            bid_evidence = infer_side_fill_evidence(
                quotes[0],
                trades=trades,
                final_depth_at_quote_shares=post_book.depth_at_price(side="BID", price=quotes[0].price),
                final_midpoint=post_quote.midpoint,
                followup_available=followup_available,
            )
            ask_evidence = infer_side_fill_evidence(
                quotes[1],
                trades=trades,
                final_depth_at_quote_shares=post_book.depth_at_price(side="ASK", price=quotes[1].price),
                final_midpoint=post_quote.midpoint,
                followup_available=followup_available,
            )
            followup = combine_fill_evidence(
                bid_evidence,
                ask_evidence,
                quote_size_shares=config.target_shares,
                observed_trade_count=len(trades),
                followup_window_seconds=evaluation.quote_observation_seconds,
                evidence_error=evidence_error,
            )
            return candidate, evaluation, quotes, followup, post_quote.midpoint, fee_cache, errors
        except Exception as exc:
            errors.append({"market_id": market_id, "error": f"{type(exc).__name__}: {exc}"})
    return None, None, None, None, None, fee_cache, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded maker spread/rebate PAPER_ONLY trial")
    parser.add_argument("--paper-db", required=True, help="new SQLite path; existing files are refused")
    parser.add_argument("--latency-seconds", type=float, default=1.0)
    parser.add_argument("--quote-observation-seconds", type=float, default=2.0)
    parser.add_argument("--target-shares", type=float, default=5.0)
    parser.add_argument("--event-limit", type=int, default=100)
    parser.add_argument("--market-limit", type=int, default=20)
    parser.add_argument("--duration-minutes", type=float, default=0.0)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    args = parser.parse_args()
    if not 0 <= args.latency_seconds <= 5:
        raise SystemExit("latency-seconds must be between 0 and 5")
    if not 0 < args.quote_observation_seconds <= 30:
        raise SystemExit("quote-observation-seconds must be between zero and 30")
    if not 0 <= args.duration_minutes <= 240:
        raise SystemExit("duration-minutes must be between 0 and 240")
    if args.duration_minutes and args.interval_seconds < 10:
        raise SystemExit("interval-seconds must be at least 10 for a bounded trial")
    path = Path(args.paper_db).resolve()
    if path.exists():
        raise SystemExit(f"refusing existing paper DB: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    duration_seconds = args.duration_minutes * 60.0
    trial_deadline = started + duration_seconds if duration_seconds else started + 180.0
    client = ReadOnlyPublicClient(timeout_seconds=5.0, deadline_monotonic=trial_deadline + 5.0)
    conn = sqlite3.connect(path)
    initialize_paper_db(conn)
    candidates = CandidateRoundRobin(
        discover_markets(
            client,
            event_limit=args.event_limit,
            market_limit=args.market_limit,
        )
    )
    if not len(candidates):
        raise SystemExit("no active maker_spread_rebate markets found in bounded sample")

    config = MakerValidationConfig(
        target_shares=args.target_shares,
        latency_seconds=args.latency_seconds,
    )
    fee_cache: dict[str, MakerFeeMetadata] = {}
    attempt = recorded = 0
    next_refresh = time.monotonic() + DISCOVERY_REFRESH_SECONDS
    try:
        while True:
            cycle_started = time.monotonic()
            attempt += 1
            if cycle_started >= next_refresh:
                try:
                    candidates.refresh(
                        discover_markets(
                            client,
                            event_limit=args.event_limit,
                            market_limit=args.market_limit,
                        )
                    )
                    print(
                        json.dumps(
                            {
                                "attempt": attempt,
                                "status": "DISCOVERY_REFRESH",
                                "candidate_count": len(candidates),
                                "discovery_generation": candidates.generation,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "attempt": attempt,
                                "status": "DISCOVERY_ERROR",
                                "error": f"{type(exc).__name__}: {exc}",
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                finally:
                    next_refresh = time.monotonic() + DISCOVERY_REFRESH_SECONDS

            candidate, evaluation, quotes, fill_followup, later_midpoint, fee_cache, errors = observe_next_candidate(
                client,
                candidates,
                fee_cache=fee_cache,
                config=config,
                quote_observation_seconds=args.quote_observation_seconds,
            )
            for error in errors:
                print(json.dumps({"attempt": attempt, "status": "READ_ERROR", **error}, sort_keys=True), flush=True)
            if candidate is not None and evaluation is not None and quotes is not None and fill_followup is not None:
                prediction_id = record_prediction(conn, evaluation)
                for quote in quotes:
                    record_hypothetical_quote(
                        conn,
                        prediction_id,
                        quote,
                        evidence={"transport": "public GET only", "queue_semantics": "existing displayed size ahead"},
                    )
                observed_trials, complete_trials = conn.execute(
                    """SELECT COUNT(*),COALESCE(SUM(two_sided_completion_state='TWO_SIDED_PROBABLE'),0)
                       FROM paper_maker_followups
                       WHERE bid_fill_evidence_state<>'UNKNOWN' AND ask_fill_evidence_state<>'UNKNOWN'"""
                ).fetchone()
                if fill_followup.bid.state != "UNKNOWN" and fill_followup.ask.state != "UNKNOWN":
                    observed_trials += 1
                    complete_trials += int(
                        fill_followup.two_sided_completion_state == "TWO_SIDED_PROBABLE"
                    )
                conservative_probability, conservative_status = conservative_fill_probability(
                    complete_evidence_trials=int(complete_trials),
                    observed_trials=int(observed_trials),
                )
                followup_id = record_followup(
                    conn,
                    prediction_id,
                    observed_at_utc=evaluation.post_quote_observed_at_utc,
                    evidence_source=fill_followup.evidence_source,
                    maker_fill_observed=None,
                    later_midpoint=later_midpoint,
                    realized_conditional_edge_usd=None,
                    fill_followup=fill_followup,
                    conservative_fill_probability=conservative_probability,
                    conservative_fill_probability_status=conservative_status,
                )
                recorded += 1
                print(
                    json.dumps(
                        {
                            "attempt": attempt,
                            "recorded": recorded,
                            "prediction_id": prediction_id,
                            "followup_id": followup_id,
                            "paper_db": str(path),
                            "event_id": candidate["event_id"],
                            "market_id": evaluation.market_id,
                            "candidate_count": len(candidates),
                            "discovery_generation": candidates.generation,
                            "status": evaluation.status,
                            "economic_status": evaluation.economic_status,
                            "spread_per_share": evaluation.spread_per_share,
                            "conditional_size_shares": evaluation.conditional_size_shares,
                            "captured_spread_usd": evaluation.captured_spread_usd,
                            "maker_rebate_usd": evaluation.maker_rebate_usd,
                            "adverse_selection_cost_usd": evaluation.adverse_selection_cost_usd,
                            "applicable_maker_fees_usd": evaluation.applicable_maker_fees_usd,
                            "conditional_maker_edge_usd": evaluation.conditional_maker_edge_usd,
                            "fill_probability_status": evaluation.fill_probability_status,
                            "bid_fill_evidence_state": fill_followup.bid.state,
                            "ask_fill_evidence_state": fill_followup.ask.state,
                            "two_sided_completion_state": fill_followup.two_sided_completion_state,
                            "inventory_risk_state": fill_followup.inventory_risk_state,
                            "hypothetical_inventory_shares": fill_followup.hypothetical_inventory_shares,
                            "conservative_fill_probability": conservative_probability,
                            "conservative_fill_probability_status": conservative_status,
                            "paper_filled_shares": evaluation.paper_filled_shares,
                            "quote_persisted": evaluation.quote_persisted_through_observation,
                            "wall_seconds": round(time.monotonic() - started, 3),
                            "execution_mode": evaluation.execution_mode,
                            "transport": "public HTTPS GET only",
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            if not duration_seconds:
                break
            remaining = trial_deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(max(0.0, args.interval_seconds - (time.monotonic() - cycle_started)), remaining))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
