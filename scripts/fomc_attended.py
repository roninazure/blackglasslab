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

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient

from parallax.direct_contracts import normalized_for_direct_contract
from parallax.economic_evidence import EconomicsEvidenceProvider
from parallax.event_discovery import discover_event_candidate
from parallax.models import Side, Venue, utcnow
from parallax.track_record import TrackRecord
from swarm_edge_runtime import RUNTIME_PATHS


TARGETS = {
    "313137": "rdc-usfed-fomc-2026-09-16-hike25",
    "313138": "rdc-usfed-fomc-2026-09-16-nochng",
}


def fred_status() -> str:
    return "AVAILABLE" if os.environ.get("FRED_API_KEY", "").strip() else "MISSING"


def discover_targets(client: PolymarketUSPublicClient):
    markets = []
    for market_id in sorted(TARGETS):
        row = client.market_by_id(market_id)
        if row.get("slug") != TARGETS[market_id]:
            raise RuntimeError(f"contract {market_id} slug mismatch")
        book_fetch_status = "book"
        try:
            book = client.book(row["slug"])
        except Exception:
            # The exact market response carries the venue BBO.  Preserve it as
            # a price-only observation when the separate book endpoint is
            # temporarily rate-limited; never manufacture depth.
            raw = dict(row.get("raw") or {})
            yes_ask = float((raw.get("bestAskQuote") or {}).get("value"))
            no_ask = 1.0 - float((raw.get("bestBidQuote") or {}).get("value"))
            raw["yes_ask"], raw["no_ask"] = yes_ask, no_ask
            row = {**row, "raw": raw}
            book = {
                f"{row['slug']}::YES": {"best_ask": yes_ask},
                f"{row['slug']}::NO": {"best_ask": no_ask},
            }
            book_fetch_status = "market_bbo_fallback"
        decision = discover_event_candidate(row, venue=Venue.POLYMARKET, book=book)
        if decision.candidate is None:
            raise RuntimeError(f"contract {market_id} rejected: {decision.reasons}")
        market = normalized_for_direct_contract(decision.candidate)
        market = replace(
            market,
            original_metadata={
                **market.original_metadata,
                "fomc_observation": True,
                "settlement_authority": "Federal Reserve",
                "settlement_reference": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
                "price_source_state": book_fetch_status,
            },
        )
        markets.append((market_id, market, decision.candidate))
    return markets


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
                "classification": f"{candidate.event_family.value}/Fed-rates/FOMC",
                "side": side.value,
                "outcome": market.outcomes[side.value],
                "price": market.yes_ask if side is Side.YES else market.no_ask,
                "verdict": "WATCH" if evidence is None else "EVALUATE",
                "observation_id": observation_id,
                "evidence": "FRED-backed observation; no certified probability" if provider.last_observation else provider.last_reason,
                "open": market.status.upper() not in {"CLOSED", "SETTLED", "RESOLVED", "FINALIZED", "EXPIRED"},
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
            try:
                report = observe_once(store, client)
                for row in report["rows"]:
                    print(
                        f"{report['observed_at']} contract={row['contract']} "
                        f"classification={row['classification']} side={row['side']} "
                        f"ask={row['price']} verdict={row['verdict']} "
                        f"observation_id={row['observation_id']} evidence={row['evidence']}",
                        flush=True,
                    )
                if not all(row["open"] for row in report["rows"]):
                    break
                cycles += 1
                if args.cycles and cycles >= args.cycles:
                    break
            except Exception as exc:  # one failed refresh must not kill attendance
                print(f"{datetime.now(UTC).isoformat()} refresh_error={type(exc).__name__}", flush=True)
            if not stopping["value"]:
                time.sleep(args.interval)
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
