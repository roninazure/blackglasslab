"""Bounded, read-only before/after probe for generic event detail enrichment."""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.event_details import fetch_kalshi_market_detail, fetch_pmus_market_detail
from parallax.event_discovery import (
    DiscoveryStatus,
    EnrichmentStatus,
    EventCandidate,
    MatchStatus,
    discover_event_candidate,
    enrich_event_candidate,
    match_event_contract,
)
from parallax.models import Venue, utcnow
from parallax.sources import KalshiPublicClient

MAX_INVENTORY_PER_VENUE = 200
MAX_DETAIL_FETCHES = 60


def _state(candidate: EventCandidate) -> MatchStatus:
    return match_event_contract(candidate, candidate).status


def _missing(candidate: EventCandidate) -> set[str]:
    identity = candidate.identity
    result = set()
    if not identity.subject:
        result.add("subject")
    if not identity.actor and not identity.body:
        result.add("actor/body")
    if not identity.action:
        result.add("action")
    if not identity.stage:
        result.add("stage")
    if not identity.deadline or not identity.temporal_scope:
        result.add("deadline/window")
    if not identity.resolution_authority:
        result.add("resolution authority")
    if not identity.outcome:
        result.add("threshold/outcome semantics")
    return result


def _summary(candidates: list[EventCandidate]) -> tuple[dict[str, int], dict[str, int]]:
    states = Counter(_state(candidate).value for candidate in candidates)
    missing = Counter(dimension for candidate in candidates for dimension in _missing(candidate))
    return (
        {state.value: states[state.value] for state in MatchStatus},
        dict(sorted(missing.items())),
    )


def _pmus_inventory(client: PolymarketUSPublicClient, limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(0, limit, 100):
        page = client.markets_page(limit=min(100, limit - len(rows)), offset=offset)
        rows.extend(page)
        if len(page) < min(100, limit - (len(rows) - len(page))):
            break
    return rows[:limit]


def run_probe(*, per_venue: int = MAX_INVENTORY_PER_VENUE, detail_cap: int = MAX_DETAIL_FETCHES) -> dict[str, Any]:
    if not 1 <= per_venue <= MAX_INVENTORY_PER_VENUE:
        raise ValueError("per-venue limit must be between 1 and 200")
    if not 0 <= detail_cap <= MAX_DETAIL_FETCHES:
        raise ValueError("detail cap must be between 0 and 60")
    started_at = utcnow()
    venue_rows: dict[Venue, list[dict[str, Any]]] = {Venue.POLYMARKET: [], Venue.KALSHI: []}
    venue_clients: dict[Venue, Any] = {}
    venue_errors: list[dict[str, str]] = []
    pmus: PolymarketUSPublicClient | None = None
    try:
        pmus = PolymarketUSPublicClient()
        venue_clients[Venue.POLYMARKET] = pmus
        try:
            venue_rows[Venue.POLYMARKET] = _pmus_inventory(pmus, per_venue)
        except Exception as exc:  # noqa: BLE001 - venue isolation is part of the probe
            venue_errors.append({"venue": "PMUS", "stage": "inventory", "error_type": type(exc).__name__})
        kalshi = KalshiPublicClient()
        venue_clients[Venue.KALSHI] = kalshi
        try:
            payload = kalshi.markets_page(limit=per_venue)
            rows = payload.get("markets", []) if isinstance(payload, dict) else []
            venue_rows[Venue.KALSHI] = [row for row in rows if isinstance(row, dict)][:per_venue]
        except Exception as exc:  # noqa: BLE001 - venue isolation is part of the probe
            venue_errors.append({"venue": "KALSHI", "stage": "inventory", "error_type": type(exc).__name__})

        candidates_by_venue: dict[Venue, list[EventCandidate]] = {Venue.POLYMARKET: [], Venue.KALSHI: []}
        for venue, rows in venue_rows.items():
            for row in rows:
                decision = discover_event_candidate(row, venue=venue, discovered_at=started_at)
                if decision.status is DiscoveryStatus.CANDIDATE and decision.candidate is not None:
                    candidates_by_venue[venue].append(decision.candidate)
        candidates = candidates_by_venue[Venue.POLYMARKET] + candidates_by_venue[Venue.KALSHI]
        before_states, before_missing = _summary(candidates)

        remaining = min(detail_cap, len(candidates))
        after: list[EventCandidate] = []
        unsupported_ids: set[tuple[Venue, str]] = set()
        per_venue_metrics: dict[Venue, Counter[str]] = {
            Venue.POLYMARKET: Counter(), Venue.KALSHI: Counter()
        }
        conversions: list[dict[str, Any]] = []
        for candidate in candidates:
            metrics = per_venue_metrics[candidate.venue]
            if remaining <= 0:
                after.append(candidate)
                continue
            remaining -= 1
            metrics["detail_fetch_attempts"] += 1
            try:
                if candidate.venue is Venue.POLYMARKET:
                    fetched = fetch_pmus_market_detail(venue_clients[candidate.venue], candidate)
                else:
                    fetched = fetch_kalshi_market_detail(venue_clients[candidate.venue], candidate)
                result = enrich_event_candidate(
                    candidate,
                    fetched.raw_response,
                    fetched_at=utcnow(),
                    source_reference=fetched.source_reference,
                )
                if result.status is EnrichmentStatus.UNCHANGED:
                    metrics["detail_fetch_failures"] += 1
                else:
                    metrics["detail_fetch_successes"] += 1
                if result.status is EnrichmentStatus.UNSUPPORTED:
                    unsupported_ids.add((candidate.venue, candidate.market_id))
                enriched = result.candidate
                before_state = _state(candidate)
                after_state = MatchStatus.UNSUPPORTED if result.status is EnrichmentStatus.UNSUPPORTED else _state(enriched)
                if before_state is not after_state or _missing(candidate) != _missing(enriched):
                    conversions.append(
                        {
                            "venue": candidate.venue.value,
                            "market_id": candidate.market_id,
                            "before": before_state.value,
                            "after": after_state.value,
                            "missing_before": sorted(_missing(candidate)),
                            "missing_after": sorted(_missing(enriched)),
                            "enrichment_reasons": list(result.reasons),
                            "conflicts": list(enriched.enrichment_conflict_flags),
                        }
                    )
                after.append(enriched)
            except Exception as exc:  # noqa: BLE001 - one detail failure must not erase the candidate
                metrics["detail_fetch_failures"] += 1
                venue_errors.append(
                    {
                        "venue": candidate.venue.value,
                        "stage": "detail",
                        "market_id": candidate.market_id,
                        "error_type": type(exc).__name__,
                    }
                )
                after.append(candidate)

        after_states = Counter(
            MatchStatus.UNSUPPORTED.value
            if (candidate.venue, candidate.market_id) in unsupported_ids
            else _state(candidate).value
            for candidate in after
        )
        after_missing = Counter(dimension for candidate in after for dimension in _missing(candidate))
        before_ambiguous = before_states[MatchStatus.AMBIGUOUS.value]
        after_ambiguous = after_states[MatchStatus.AMBIGUOUS.value]
        ambiguity_reduction = (
            (before_ambiguous - after_ambiguous) / before_ambiguous
            if before_ambiguous else 0.0
        )
        venues = {}
        for venue, label in ((Venue.POLYMARKET, "PMUS"), (Venue.KALSHI, "KALSHI")):
            metrics = per_venue_metrics[venue]
            venues[label] = {
                "inventory_inspected": len(venue_rows[venue]),
                "candidates": len(candidates_by_venue[venue]),
                "detail_fetch_attempts": metrics["detail_fetch_attempts"],
                "detail_fetch_successes": metrics["detail_fetch_successes"],
                "detail_fetch_failures": metrics["detail_fetch_failures"],
            }
        return {
            "started_at": started_at.isoformat(),
            "completed_at": utcnow().isoformat(),
            "bounds": {"inventory_per_venue": per_venue, "detail_fetch_absolute_cap": detail_cap},
            "venues": venues,
            "before": {"states": before_states, "missing_dimensions": before_missing},
            "after": {
                "states": {state.value: after_states[state.value] for state in MatchStatus},
                "missing_dimensions": dict(sorted(after_missing.items())),
            },
            "ambiguity_reduction": ambiguity_reduction,
            "representative_conversions": conversions[:10],
            "errors": venue_errors,
            "read_only": True,
            "orders": 0,
            "alerts": 0,
            "published": 0,
            "durable_writes": 0,
        }
    finally:
        if pmus is not None:
            pmus.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-venue", type=int, default=MAX_INVENTORY_PER_VENUE)
    parser.add_argument("--detail-cap", type=int, default=MAX_DETAIL_FETCHES)
    args = parser.parse_args()
    print(json.dumps(run_probe(per_venue=args.per_venue, detail_cap=args.detail_cap), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
