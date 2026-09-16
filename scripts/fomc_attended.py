#!/usr/bin/env python3
"""Attended, read-only observer for the 2026-09-16 PMUS FOMC contracts.

This lane is deliberately exact-contract and probability-free.  It captures
both sides of each named contract into the normal immutable prospective ledger
and refreshes until both contracts are no longer open.
"""

from __future__ import annotations

import argparse
import os
import signal
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient, _amount

from parallax.direct_contracts import normalized_for_direct_contract
from parallax.economic_evidence import EconomicsEvidenceProvider
from parallax.event_discovery import classify_event_family, discover_event_candidate
from parallax.generic_events import normalize_binary_event
from parallax.models import Side, Venue, utcnow
from parallax.track_record import TrackRecord
from swarm_edge_runtime import RUNTIME_PATHS


TARGETS = {
    "313137": "rdc-usfed-fomc-2026-09-16-hike25",
    "313138": "rdc-usfed-fomc-2026-09-16-nochng",
}


def fred_status() -> str:
    return "AVAILABLE" if os.environ.get("FRED_API_KEY", "").strip() else "MISSING"


def target_status(row: dict) -> str:
    raw = row.get("raw") or {}
    if row.get("closed") is True:
        return "CLOSED"
    # These are venue fields observed on the exact contracts, not quote-derived
    # resolution guesses.  Missing quotes alone do not imply closure.
    if raw.get("ep3Status") == "EXPIRED":
        return "EXPIRED"
    status = str(raw.get("status") or "").removeprefix("MARKET_STATUS_")
    if status:
        return status
    return "OPEN" if row.get("active") and row.get("accepting_orders") else "UNKNOWN"


def market_bbo(row: dict, *, executable: bool) -> dict:
    raw = row.get("raw") or {}

    def quote(name: str) -> float | None:
        value = raw.get(name)
        if value is None or (isinstance(value, dict) and value.get("value") is None):
            return None
        return _amount(value, name=name)

    bid, ask = quote("bestBidQuote"), quote("bestAskQuote")
    return {
        f"{row['slug']}::YES": {
            "best_bid": bid,
            "best_ask": ask if executable else None,
        },
        f"{row['slug']}::NO": {
            "best_bid": 1.0 - ask if executable and ask is not None else None,
            "best_ask": 1.0 - bid if executable and bid is not None else None,
        },
    }


def discover_targets(client: PolymarketUSPublicClient):
    markets = []
    for market_id in sorted(TARGETS):
        row = client.market_by_id(market_id)
        if row.get("slug") != TARGETS[market_id]:
            raise RuntimeError(f"contract {market_id} slug mismatch")
        status = target_status(row)
        book_fetch_status = "book"
        candidate = None
        if status != "OPEN":
            # Closed contracts are intentionally rejected by trading discovery.
            # This exact-contract observer still records their terminal state.
            book = market_bbo(row, executable=False)
            market = normalize_binary_event(
                row, venue=Venue.POLYMARKET,
                event_id=f"direct-contract:POLYMARKET:{market_id}",
                event_family=classify_event_family(row).value,
                event_question=row["question"],
                resolution_reference=row["slug"], book=book,
            )
            book_fetch_status = "market_bbo_non_open"
        else:
            market, candidate, book_fetch_status = discover_open_target(client, row)
        market = replace(
            market, status=status,
            original_metadata={
                **market.original_metadata,
                "fomc_observation": True,
                "settlement_authority": "Federal Reserve",
                "settlement_reference": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                "price_source_state": book_fetch_status,
            },
        )
        markets.append((market_id, market, candidate))
    return markets


def discover_open_target(client: PolymarketUSPublicClient, row: dict):
    book_fetch_status = "book"
    try:
        book = client.book(row["slug"])
    except Exception:
        # The exact market response carries the venue BBO.  Preserve it as
        # a price-only observation when the separate book endpoint is
        # temporarily rate-limited; never manufacture depth.
        book = market_bbo(row, executable=True)
        book_fetch_status = "market_bbo_fallback"
    decision = discover_event_candidate(row, venue=Venue.POLYMARKET, book=book)
    if decision.candidate is None:
        raise RuntimeError(f"contract {row['id']} rejected: {decision.reasons}")
    market = normalized_for_direct_contract(decision.candidate)
    return market, decision.candidate, book_fetch_status


def observe_once(store: TrackRecord, client: PolymarketUSPublicClient) -> dict:
    captured = []
    provider = EconomicsEvidenceProvider()
    for market_id, market, candidate in discover_targets(client):
        evidence = provider.assess(market)  # May return None; never infer a probability.
        for side in (Side.YES, Side.NO):
            observation = store.capture_prospective(market, side, evidence, now=utcnow())
            observation_id = observation["observation_id"]
            if store.prospective_record(observation_id) is None:
                raise RuntimeError(f"durable read-back failed for {market_id}")
            captured.append({
                "contract": market_id,
                "classification": f"{market.category}/Fed-rates/FOMC",
                "side": side.value,
                "outcome": market.outcomes[side.value],
                "price": market.yes_ask if side is Side.YES else market.no_ask,
                "bid": market.yes_bid if side is Side.YES else market.no_bid,
                "status": market.status,
                "verdict": "WATCH" if evidence is None else "EVALUATE",
                "observation_id": observation_id,
                "evidence": "FRED-backed observation; no certified probability" if provider.last_observation else provider.last_reason,
                "open": market.status == "OPEN",
            })
    return {"observed_at": datetime.now(UTC).isoformat(), "fred_api_key": fred_status(), "rows": captured}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--cycles", type=int, default=0, help="bounded validation cycles; 0 means monitor until resolution")
    parser.add_argument("--db", type=Path, default=RUNTIME_PATHS.root / "data/parallax-commercial/prospective.sqlite")
    args = parser.parse_args()
    if args.interval < 30:
        parser.error("--interval must be at least 30 seconds")
    if args.cycles < 0:
        parser.error("--cycles cannot be negative")
    store = TrackRecord(args.db)
    client = PolymarketUSPublicClient()
    stopping = {"value": False}
    signal.signal(signal.SIGINT, lambda *_: stopping.__setitem__("value", True))
    signal.signal(signal.SIGTERM, lambda *_: stopping.__setitem__("value", True))
    cycles = 0
    try:
        while not stopping["value"]:
            cycles += 1
            try:
                report = observe_once(store, client)
                for row in report["rows"]:
                    print(
                        f"{report['observed_at']} contract={row['contract']} "
                        f"classification={row['classification']} side={row['side']} "
                        f"ask={row['price']} verdict={row['verdict']} "
                        f"bid={row['bid']} status={row['status']} open={row['open']} "
                        f"observation_id={row['observation_id']} evidence={row['evidence']}",
                        flush=True,
                    )
                if not any(row["open"] for row in report["rows"]):
                    break
            except Exception as exc:  # one failed refresh must not kill attendance
                print(f"{datetime.now(UTC).isoformat()} refresh_error={type(exc).__name__}", flush=True)
            if args.cycles and cycles >= args.cycles:
                break
            if not stopping["value"]:
                time.sleep(args.interval)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
