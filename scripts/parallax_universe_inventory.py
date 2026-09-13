"""One bounded, read-only PARALLAX venue inventory scan."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.discovery import MAX_ACTIVE_MARKETS_PER_VENUE, Coverage, classify_market, deduplicate, inventory, normalize_market, paginate, paginate_collection
from parallax.sources import KalshiPublicClient


def main() -> None:
    failures: list[dict[str, str]] = []
    scope_notes: list[dict[str, str]] = []
    # PMUS SDK currently exposes no event-list resource. Page its active public
    # market collection to the hard ceiling and report that limitation.
    pmus = PolymarketUSPublicClient()
    try:
        pmus_rows, pmus_coverage = paginate(pmus.markets_page, page_size=100, max_pages=100, max_rows=MAX_ACTIVE_MARKETS_PER_VENUE)
    finally:
        pmus.close()
    pmus_coverage = pmus_coverage.__class__(pmus_coverage.state, pmus_coverage.pages, pmus_coverage.rows_returned, pmus_coverage.unique_rows, pmus_coverage.page_limit, pmus_coverage.max_pages, "PMUS public SDK has no event-list resource; bounded active-market pagination", pmus_coverage.upstream_empty)
    kalshi = KalshiPublicClient()
    # Operational bound for one inventory run: the first deterministic active
    # series page and one page per event/market scope. This is intentionally
    # BOUNDED and avoids an unbounded request fan-out across thousands of scopes.
    series_rows, series_coverage = paginate_collection(kalshi.series_page, key="series", max_pages=1)
    kalshi_rows: list[dict] = []
    seen_events: set[str] = set()
    for series in series_rows[:10]:
        series_id = str(series.get("ticker") or series.get("series_ticker") or "")
        if not series_id:
            continue
        try:
            events, _ = paginate_collection(lambda **p: kalshi.events_page(series_ticker=series_id, **p), key="events", max_pages=1)
        except Exception as exc:
            failures.append({"scope": series_id, "stage": "events", "error": type(exc).__name__})
            continue
        for event in events:
            event_id = str(event.get("event_ticker") or event.get("ticker") or event.get("id") or "")
            if not event_id or event_id in seen_events:
                continue
            seen_events.add(event_id)
            try:
                rows, _ = paginate_collection(lambda **p: kalshi.event_markets_page(event_ticker=event_id, **p), key="markets", max_pages=1, max_rows=MAX_ACTIVE_MARKETS_PER_VENUE - len(kalshi_rows))
            except Exception as exc:
                failures.append({"scope": event_id, "stage": "markets", "error": type(exc).__name__})
                continue
            for row_value in rows:
                kalshi_rows.append({**row_value, "_discovery_event": event, "_discovery_series": series})
            if len(kalshi_rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
                break
        if len(kalshi_rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
            break
    kalshi_coverage = series_coverage.__class__(series_coverage.state if len(kalshi_rows) < MAX_ACTIVE_MARKETS_PER_VENUE else Coverage.BOUNDED, series_coverage.pages, len(kalshi_rows), len({str(x.get("ticker") or x.get("id")) for x in kalshi_rows}), series_coverage.page_limit, series_coverage.max_pages, "series/event-scoped traversal; see failed scopes" if failures else "series/event-scoped traversal completed", not kalshi_rows)
    markets = [classify_market(normalize_market(row.get("raw", row), venue="PMUS", event=row.get("_discovery_event"))) for row in pmus_rows]
    # The market page already carries event/series IDs. Detail calls are not
    # needed for the bounded inventory and would multiply the read budget.
    markets += [classify_market(normalize_market(row, venue="KALSHI", event=row.get("_discovery_event"), series=row.get("_discovery_series"))) for row in kalshi_rows if row.get("ticker")]
    markets = deduplicate(markets)
    report = inventory(markets)
    report.update({"coverage": {"PMUS": pmus_coverage.__dict__, "KALSHI": kalshi_coverage.__dict__}, "raw_counts": dict(Counter(x.venue for x in markets)), "failed_truncated_scopes": failures, "scope_notes": scope_notes, "read_only": True, "orders": 0, "alerts": 0, "published": 0})
    print(json.dumps(report, default=lambda x: x.value if hasattr(x, "value") else x, allow_nan=False))


if __name__ == "__main__":
    main()
