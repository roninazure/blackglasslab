"""Bounded, read-only CFB V1 PMUS/Kalshi acceptance scan."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient
from parallax.cfb import VALIDATION_ECE, CFBEvidenceProvider, fetch_games, is_supported_market, map_market_to_game, probability_for_game
from parallax.discovery import MAX_ACTIVE_MARKETS_PER_VENUE, paginate, paginate_collection
from parallax.economics import retail_example
from parallax.engine import qualify
from parallax.fees import attach_fees
from parallax.models import Side, Venue, utcnow
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.sources import KalshiPublicClient

CFB_MARKET_DISCOVERY_LIMIT = 1_000


def _text(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str).lower()


def _is_cfb(row: dict, *parents: dict) -> bool:
    text = " ".join(_text(x) for x in (row, *parents))
    return bool(re.search(r"\b(?:cfb|ncaaf|college football)\b|kx(?:ncaaf|cfb)", text, re.I))


def _scope_kalshi(client: KalshiPublicClient) -> tuple[list[dict], dict]:
    series, coverage = paginate_collection(client.series_page, key="series", max_pages=100)
    cfb_series = [s for s in series if str(s.get("ticker") or "").upper() in {"KXNCAAFGAME", "KXCFBGAME"}]
    rows: list[dict] = []
    failures: list[str] = []
    for series_row in cfb_series:
        sid = str(series_row.get("ticker") or series_row.get("id") or "")
        try:
            events, _ = paginate_collection(lambda *, limit, cursor, sid=sid: client.events_page(series_ticker=sid, limit=limit, cursor=cursor), key="events", max_pages=100, max_rows=MAX_ACTIVE_MARKETS_PER_VENUE)
            for event in events:
                eid = str(event.get("ticker") or event.get("event_ticker") or event.get("id") or "")
                markets, _ = paginate_collection(lambda *, limit, cursor, eid=eid: client.event_markets_page(event_ticker=eid, limit=limit, cursor=cursor), key="markets", max_pages=100, max_rows=max(1, MAX_ACTIVE_MARKETS_PER_VENUE - len(rows)))
                rows.extend({**market, "_discovery_event": event, "_discovery_series": series_row} for market in markets)
                if len(rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
                    break
        except Exception as exc:  # bounded failure summary only
            failures.append(type(exc).__name__)
        if len(rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
            break
    return rows, {"state": "BOUNDED" if len(rows) >= MAX_ACTIVE_MARKETS_PER_VENUE else ("PARTIAL" if failures else "COMPLETE"), "series_pages": coverage.pages, "cfb_series": len(cfb_series), "failed_scopes": Counter(failures)}


def _scan() -> dict:
    current_year = datetime.now(UTC).year
    seasons = tuple(range(2010, current_year + 1))
    games = fetch_games(seasons)
    result = {"read_only": True, "orders": 0, "alerts": 0, "published": 0, "cfbd_calls": len(seasons), "validation_ece": VALIDATION_ECE, "venues": {}, "rows": [], "mapping_failure_reasons": Counter()}
    pmus_client = PolymarketUSPublicClient()
    try:
        raw_pmus, coverage = paginate(pmus_client.markets_page, page_size=100, max_pages=10, max_rows=CFB_MARKET_DISCOVERY_LIMIT)
        pmus_rows = [r for r in raw_pmus if r.get("active") and not r.get("closed") and _is_cfb(r, r.get("raw", {}))]
        result["venues"]["PMUS"] = {"coverage": coverage.__dict__, "discovered": len(pmus_rows)}
    finally:
        pmus_client.close()
    kalshi_client = KalshiPublicClient()
    try:
        kalshi_rows, coverage = _scope_kalshi(kalshi_client)
        result["venues"]["KALSHI"] = {"coverage": coverage, "discovered": len(kalshi_rows), "endpoint_status": coverage["state"]}
    except Exception as exc:
        kalshi_rows = []
        result["venues"]["KALSHI"] = {"coverage": {"state": "PARTIAL", "error_type": type(exc).__name__}, "discovered": 0, "endpoint_status": "UNAVAILABLE"}

    statuses: dict[str, Counter] = {"PMUS": Counter(), "KALSHI": Counter()}
    scored: list[dict] = []
    for venue_name, raw_rows in (("PMUS", pmus_rows), ("KALSHI", kalshi_rows)):
        for raw in raw_rows:
            event = raw.get("_discovery_event") if venue_name == "KALSHI" else None
            series = raw.get("_discovery_series") if venue_name == "KALSHI" else None
            try:
                market = normalize_pmus(raw, {}, utcnow().isoformat()) if venue_name == "PMUS" else normalize_kalshi(raw, {}, utcnow().isoformat(), event=event)
            except (KeyError, TypeError, ValueError):
                statuses[venue_name]["MALFORMED"] += 1
                continue
            if not is_supported_market(market):
                mapping = map_market_to_game(market, games)
                statuses[venue_name][mapping.status] += 1
                result["mapping_failure_reasons"][mapping.status] += 1
                continue
            statuses[venue_name]["ELIGIBLE_MONEYLINE"] += 1
            mapping = map_market_to_game(market, games)
            statuses[venue_name][mapping.status] += 1
            if mapping.status != "MAPPED" or mapping.game is None:
                result["mapping_failure_reasons"][mapping.status] += 1
                continue
            try:
                if venue_name == "PMUS":
                    client = PolymarketUSPublicClient(timeout_seconds=8)
                    try:
                        book = client.book(raw["slug"])
                    finally:
                        client.close()
                    market = normalize_pmus(raw, book, utcnow().isoformat())
                    market = attach_fees(market, utcnow())
                else:
                    book = kalshi_client.book(raw["ticker"])
                    market = attach_fees(normalize_kalshi(raw, book, utcnow().isoformat(), event=event), utcnow(), event=event, series=series)
                statuses[venue_name]["EXECUTABLE_BOOK"] += 1
                evidence = CFBEvidenceProvider(lambda games=games: games).assess(market)
                if evidence is None:
                    statuses[venue_name]["NO_EVIDENCE"] += 1
                    continue
                statuses[venue_name]["EVIDENCE"] += 1
                for side in Side:
                    play = qualify(market, side, evidence, now=utcnow())
                    example_rows = {}
                    for index, stake in ((1, 25), (2, 50), (3, 100)):
                        ex = play.retail_examples[index]
                        example_rows[f"net_ev_{stake}"] = (play.model_probability * ex.estimated_payout_if_correct - ex.total_cost) if play.model_probability is not None and ex.available and ex.total_cost is not None else None
                    row = {"venue": venue_name, "market_id": market.venue_market_id, "matchup": f"{mapping.game.away_team} at {mapping.game.home_team}", "kickoff_utc": mapping.game.kickoff, "side": side.value, "cfb_v1_probability": play.model_probability if side == Side.YES else (1 - play.model_probability if play.model_probability is not None else None), "executable_price": play.executable_price, "raw_edge": play.edge_points, "cfb_safety": play.edge_points is not None and play.edge_points > VALIDATION_ECE + 1e-9, "fee": play.fees_estimate, "fee_status": market.mechanics.fee_status, "net_ev_25": play.expected_value, **example_rows, "max_loss": play.retail_examples[1].maximum_loss, "payout": play.retail_examples[1].estimated_payout_if_correct, "liquidity": play.executable_size, "verdict": play.suggested_action.value}
                    scored.append(row)
                    statuses[venue_name]["FULLY_SCORED_SIDES"] += 1
                    statuses[venue_name][play.suggested_action.value] += 1
            except Exception as exc:
                statuses[venue_name][f"SCORING_{type(exc).__name__}"] += 1
    for venue, data in result["venues"].items():
        counts = statuses[venue]
        result["venues"][venue].update({"eligible": counts["ELIGIBLE_MONEYLINE"], "mapped": counts["MAPPED"], "evidence": counts["EVIDENCE"], "model_probability": counts["EVIDENCE"], "executable_book": counts["EXECUTABLE_BOOK"], "fee": sum(1 for row in scored if row["venue"] == venue and row["fee_status"] in {"VERIFIED_UPPER_BOUND", "VERIFIED_SCHEDULE", "REVIEWED"}), "edge": sum(1 for row in scored if row["venue"] == venue and row["raw_edge"] is not None), "economics": sum(1 for row in scored if row["venue"] == venue and row["net_ev_25"] is not None), "fully_scored_sides": counts["FULLY_SCORED_SIDES"], "BUY": counts["BUY"], "WATCH": counts["WATCH"], "PASS": counts["PASS"], "status_counts": dict(counts)})
    result["rows"] = sorted(scored, key=lambda row: (row["verdict"] != "BUY", -(row["raw_edge"] or -1)))[:50]
    result["mapping_failure_reasons"] = dict(result["mapping_failure_reasons"])
    return result


if __name__ == "__main__":
    print(json.dumps(_scan(), default=lambda value: value.value if hasattr(value, "value") else value, allow_nan=False))
