"""One bounded, read-only NFL-scoped live inventory scan."""
from __future__ import annotations

import json
import re
import signal
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import PolymarketUSPublicClient, redact_sensitive
from parallax.alerts import AlertDeliveryStore, AlertDispatcher, dispatch_scored_buy
from parallax.discovery import MAX_ACTIVE_MARKETS_PER_VENUE, paginate, paginate_collection
from parallax.economics import retail_example
from parallax.engine import qualify
from parallax.fees import attach_fees
from parallax.inbox import default_inbox_store
from parallax.models import Action, Side, utcnow
from parallax.nfl import VALIDATION_ECE, NFLEvidenceProvider, fetch_games, is_supported_market, map_market_to_game, probability_for_game
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.sources import KalshiPublicClient
from parallax.slate import reconcile_slate
from parallax.track_record import TrackRecord

PROSPECTIVE_DB = Path("data/parallax-commercial/prospective.sqlite")
NFL_SLATE_HORIZON_DAYS = 7
NFL_TZ = ZoneInfo("America/New_York")


def _upcoming_slate(games, now):
    """Return authoritative NFL dates in the rolling horizon, preserving started games."""
    today = now.astimezone(NFL_TZ).date()
    final_date = today + timedelta(days=NFL_SLATE_HORIZON_DAYS)
    scheduled = []
    for game in games:
        kickoff = datetime.fromisoformat(game.kickoff.replace("Z", "+00:00"))
        local_date = kickoff.astimezone(NFL_TZ).date()
        if local_date < today or local_date > final_date:
            continue
        scheduled.append(
            {
                "game_id": game.game_id,
                "date": local_date.isoformat(),
                "start_time": kickoff.isoformat(),
                "away_team": game.away_team,
                "home_team": game.home_team,
                "schedule_status": "PAST_START" if kickoff <= now else "SCHEDULED",
            }
        )
    return scheduled


def _capture_evaluated(store, market, side, evidence, now):
    """Capture exactly one already-qualified live market-side observation."""
    play = qualify(market, side, evidence, now=now)
    observation = store.capture_prospective(market, side, evidence, now=now)
    observation_id = observation.get("observation_id") if isinstance(observation, dict) else None
    expected = (play.id, play.venue, play.market_id, play.side)
    actual = tuple(observation.get(key) for key in ("play_id", "venue", "market_id", "side")) if isinstance(observation, dict) else None
    if not observation_id or actual != expected or store.prospective_record(observation_id) is None:
        raise ValueError("Prospective capture did not produce a verified durable observation ID")
    return play


def _dispatch_buy_alert(dispatcher, play, market, mapping, detected_at):
    """Send one immediate deduplicated ntfy alert for a scored NFL BUY."""
    if play.suggested_action != Action.BUY:
        return None
    matchup = (
        f"{mapping.game.away_team} at {mapping.game.home_team}"
        if mapping.game
        else market.title
    )
    return dispatch_scored_buy(
        dispatcher,
        play,
        market,
        sport="NFL",
        matchup=matchup,
        detected_at=detected_at.isoformat(),
    )


def _text(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str).lower()


def _is_nfl(row: dict, *parents: dict) -> bool:
    """Scope using venue metadata/ticker first, then a narrow text fallback."""
    explicit = " ".join(_text(parent) for parent in parents)
    explicit += " " + " ".join(str(row.get(k) or "") for k in ("category", "sport", "league", "series_ticker", "event_ticker", "marketType", "sportsMarketTypeV2"))
    return bool(re.search(r"\bnfl\b|kx?nfl", explicit, re.I) or re.search(r"\bnfl\b", _text(row), re.I))


def _scoped_call(function, **kwargs):
    """Bound one slow public scope request without hiding it as empty."""
    def timeout(_signum, _frame):
        raise TimeoutError("public scope request exceeded scan timeout")
    signal.signal(signal.SIGALRM, timeout)
    signal.alarm(12)
    try:
        return function(**kwargs)
    finally:
        signal.alarm(0)


def _scope_kalshi(client: KalshiPublicClient) -> tuple[list[dict], dict]:
    series, series_coverage = paginate_collection(lambda **kwargs: _scoped_call(client.series_page, **kwargs), key="series", max_pages=100)
    nfl_series = [s for s in series if _is_nfl(s)]
    rows: list[dict] = []
    failures: list[dict] = []
    events_seen = 0
    for series_row in nfl_series:
        sid = str(series_row.get("ticker") or series_row.get("id") or "")
        try:
            events, event_cov = paginate_collection(
                lambda *, limit, cursor, sid=sid: _scoped_call(client.events_page, series_ticker=sid, limit=limit, cursor=cursor),
                key="events", max_pages=100, max_rows=MAX_ACTIVE_MARKETS_PER_VENUE,
            )
        except Exception as exc:
            failures.append({"scope": sid, "stage": "events", "error": type(exc).__name__})
            continue
        if event_cov.state.value != "COMPLETE":
            failures.append({"scope": sid, "stage": "events", "state": event_cov.state.value, "reason": event_cov.reason})
        for event in events:
            events_seen += 1
            eid = str(event.get("ticker") or event.get("event_ticker") or event.get("id") or "")
            try:
                markets, market_cov = paginate_collection(
                    lambda *, limit, cursor, eid=eid: _scoped_call(client.event_markets_page, event_ticker=eid, limit=limit, cursor=cursor),
                    key="markets", max_pages=100, max_rows=max(1, MAX_ACTIVE_MARKETS_PER_VENUE - len(rows)),
                )
            except Exception as exc:
                failures.append({"scope": eid, "stage": "markets", "error": type(exc).__name__})
                continue
            if market_cov.state.value != "COMPLETE":
                failures.append({"scope": eid, "stage": "markets", "state": market_cov.state.value, "reason": market_cov.reason})
            rows.extend({**market, "_discovery_event": event, "_discovery_series": series_row} for market in markets)
            if len(rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
                break
        if len(rows) >= MAX_ACTIVE_MARKETS_PER_VENUE:
            break
    unique = {}
    for row in rows:
        key = str(row.get("ticker") or row.get("id") or "")
        if key:
            unique.setdefault(key, row)
    ceiling = len(unique) >= MAX_ACTIVE_MARKETS_PER_VENUE
    state = "BOUNDED" if ceiling else ("PARTIAL" if failures or series_coverage.state.value != "COMPLETE" else "COMPLETE")
    return list(unique.values()), {"state": state, "series_pages": series_coverage.pages, "series_observed": len(series), "nfl_series": len(nfl_series), "events_observed": events_seen, "markets_returned": len(rows), "unique_markets": len(unique), "failed_scopes": failures, "series_coverage_reason": series_coverage.reason}


def _scan() -> dict:
    games = fetch_games()
    result = {"read_only": True, "orders": 0, "alerts": 0, "published": 0, "prospective_captured": 0, "validation_ece": VALIDATION_ECE, "venues": {}}
    prospective_store = TrackRecord(PROSPECTIVE_DB)
    alert_dispatcher = AlertDispatcher(
        AlertDeliveryStore(default_inbox_store().path)
    )
    pmus_rows: list[dict] = []
    pmus_discovery_complete = False
    kalshi_discovery_complete = False
    pmus = PolymarketUSPublicClient()
    try:
        raw_pmus, cov = paginate(pmus.markets_page, page_size=100, max_pages=100, max_rows=MAX_ACTIVE_MARKETS_PER_VENUE)
        pmus_rows = [r for r in raw_pmus if r.get("active") and not r.get("closed") and _is_nfl(r, r.get("raw", {}))]
        result["venues"]["PMUS"] = {"coverage": cov.__dict__, "universe_rows": len(raw_pmus), "nfl_rows": len(pmus_rows)}
        pmus_discovery_complete = cov.state.value == "COMPLETE"
    except Exception as exc:
        result["venues"]["PMUS"] = {
            "coverage": {"state": "PARTIAL", "reason": "market discovery unavailable"},
            "universe_rows": 0, "nfl_rows": 0,
            "failure": {
                "timestamp": utcnow().isoformat(), "venue": "PMUS",
                "context": "NFL prospective market discovery",
                "classification": type(exc).__name__,
                "underlying_error": redact_sensitive(getattr(exc, "underlying_error", exc)),
                "attempt_count": getattr(exc, "attempts", 1),
                "status": "DATA_UNAVAILABLE / NO_VALID_OBSERVATION",
            },
        }
    finally:
        pmus.close()
    kalshi = KalshiPublicClient()
    try:
        kalshi_rows, kalshi_cov = _scope_kalshi(kalshi)
        result["venues"]["KALSHI"] = {"coverage": kalshi_cov, "nfl_rows": len(kalshi_rows)}
        kalshi_discovery_complete = kalshi_cov.get("state") == "COMPLETE"
    except Exception as exc:
        kalshi_rows, kalshi_cov = [], {"state": "PARTIAL", "failed_scopes": [{"stage": "series", "error": type(exc).__name__}]}
        result["venues"]["KALSHI"] = {"coverage": kalshi_cov, "nfl_rows": 0}
    statuses = Counter()
    rejection_reasons = Counter()
    rows_out = []
    data_unavailable_game_ids: set[str] = set()
    mapping_failure_game_ids: set[str] = set()
    for venue, raw_rows in (("PMUS", pmus_rows), ("KALSHI", kalshi_rows)):
        for raw in raw_rows:
            try:
                event = raw.get("_discovery_event") if venue == "KALSHI" else None
                if venue == "PMUS":
                    market = normalize_pmus(raw, {}, utcnow().isoformat())
                else:
                    market = normalize_kalshi(raw, {}, "live-scan", event=event)
            except (KeyError, TypeError, ValueError):
                statuses["MALFORMED"] += 1
                continue
            if not is_supported_market(market):
                rejection_reasons["NON_GAME_WINNER_OR_DERIVATIVE"] += 1
                continue
            mapping = map_market_to_game(market, games)
            statuses[mapping.status] += 1
            row = {"venue": venue, "market_id": market.venue_market_id, "status": mapping.status, "reason": mapping.reason, "title": market.title}
            if mapping.game:
                row["game_id"] = mapping.game.game_id
                row["game"] = f"{mapping.game.away_team} at {mapping.game.home_team}"
                row["kickoff"] = mapping.game.kickoff
                if mapping.status not in {"MAPPED_GAME_WINNER", "PAST_START"}:
                    mapping_failure_game_ids.add(mapping.game.game_id)
            if mapping.status == "MAPPED_GAME_WINNER" and mapping.game:
                row["model_probability"] = probability_for_game(mapping.game, games)
                try:
                    if venue == "PMUS":
                        client = PolymarketUSPublicClient(timeout_seconds=8)
                        try:
                            book = client.book(raw["slug"])
                        finally:
                            client.close()
                        market = attach_fees(normalize_pmus(raw, book, utcnow().isoformat()), utcnow())
                    else:
                        # Kalshi discovery rows are normalized with their public book
                        # below when the venue exposes one; failures stay explicit.
                        book = KalshiPublicClient().book(raw["ticker"])
                        market = attach_fees(normalize_kalshi(raw, book, utcnow().isoformat(), event=event), utcnow(), event=event, series=raw.get("_discovery_series"))
                    evidence = NFLEvidenceProvider(lambda: games).assess(market)
                    if evidence is None:
                        statuses["EVIDENCE_MISSING"] += 1
                        data_unavailable_game_ids.add(mapping.game.game_id)
                    else:
                        statuses["EVIDENCE"] += 1
                        for side in Side:
                            decision_at = utcnow()
                            play = _capture_evaluated(
                                prospective_store, market, side, evidence, decision_at
                            )
                            result["prospective_captured"] += 1
                            statuses[f"SCORED_{play.suggested_action}"] += 1
                            try:
                                alert_result = _dispatch_buy_alert(
                                    alert_dispatcher,
                                    play,
                                    market,
                                    mapping,
                                    decision_at,
                                )
                                if (
                                    alert_result is not None
                                    and alert_result["status"] == "SENT"
                                    and not alert_result["deduplicated"]
                                ):
                                    result["alerts"] += 1
                            except Exception:
                                statuses["ALERT_ERROR"] += 1
                            scored = {"venue": venue, "market": market.title, "market_id": market.venue_market_id, "game_id": mapping.game.game_id, "side": side.value, "game_start": mapping.game.kickoff, "nfl_v1_probability": play.model_probability, "executable_price": play.executable_price, "raw_edge": play.edge_points, "safety_margin": (play.edge_points / 100 - VALIDATION_ECE) if play.edge_points is not None else None, "fee": play.fees_estimate, "net_ev_25": play.expected_value, "net_ev_50": None, "net_ev_100": None, "liquidity": play.executable_size, "failed_gates": list(play.verdict.failed_gates), "verdict": play.suggested_action.value}
                            for index, key in ((2, "net_ev_50"), (3, "net_ev_100")):
                                example = play.retail_examples[index]
                                scored[key] = (play.model_probability * example.estimated_payout_if_correct - example.total_cost) if play.model_probability is not None and example.available and example.total_cost is not None else None
                            rows_out.append(scored)
                except Exception as exc:
                    statuses["BOOK_OR_SCORING_ERROR"] += 1
                    data_unavailable_game_ids.add(mapping.game.game_id)
                    row["scoring_error"] = type(exc).__name__
            rows_out.append(row)
    scheduled = _upcoming_slate(games, utcnow())
    result["slate"] = reconcile_slate(
        scheduled,
        rows_out,
        discovery_complete=pmus_discovery_complete and kalshi_discovery_complete,
        data_unavailable_game_ids=data_unavailable_game_ids,
        mapping_failure_game_ids=mapping_failure_game_ids,
    )
    result["summary"] = {"nfl_markets_discovered": {"PMUS": len(pmus_rows), "KALSHI": len(kalshi_rows)}, "status_counts": dict(statuses), "rejection_counts": dict(rejection_reasons), "rows": rows_out}
    return result


if __name__ == "__main__":
    print(json.dumps(_scan(), default=lambda value: value.value if hasattr(value, "value") else value, allow_nan=False))
