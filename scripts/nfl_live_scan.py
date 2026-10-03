"""One bounded, read-only NFL-scoped live inventory scan."""
from __future__ import annotations

import json
import math
import re
import signal
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maker_spread_economics.polymarket_us import (
    PolymarketUSPublicClient,
    PolymarketUSRateLimit,
    redact_sensitive,
)
from parallax.alerts import (
    AlertDeliveryStore,
    AlertDispatcher,
    dispatch_scored_buy,
    reconcile_active_buy_alerts,
)
from parallax.discovery import (
    paginate_collection,
)
from parallax.economics import retail_example
from parallax.engine import qualify
from parallax.fees import attach_fees
from parallax.inbox import default_inbox_store
from parallax.models import Action, Side, timestamp, utcnow
from parallax.nfl import (
    VALIDATION_ECE,
    NFLEvidenceProvider,
    NFL_TEAM_NAMES,
    economic_team_for_side,
    fetch_games,
    market_support_reason,
    map_market_to_game,
    probability_for_game,
)
from parallax.normalization import normalize_kalshi, normalize_pmus
from parallax.pmus_acquisition import PMUSAcquisition
from parallax.slate import reconcile_slate
from parallax.sources import KalshiPublicClient
from parallax.track_record import TrackRecord

PROSPECTIVE_DB = Path("data/parallax-commercial/prospective.sqlite")
NFL_SLATE_HORIZON_DAYS = 7
NFL_TZ = ZoneInfo("America/New_York")
KALSHI_NFL_SERIES_TICKER = "KXNFLGAME"
KALSHI_NFL_DISCOVERY_MAX_REQUESTS = 12
PMUS_NFL_MAX_SLUGS = 100

# nflverse uses LA for the Rams; both venues use LAR in contract identity.
NFL_VENUE_TEAM_CODES = {"LA": "LAR"}


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


def _venue_team_code(value: object) -> str:
    code = str(value or "").strip().upper()
    return NFL_VENUE_TEAM_CODES.get(code, code)


def _pmus_slugs(scheduled: list[dict]) -> list[str]:
    """Construct the two observed PMUS NFL slug forms from schedule identity."""
    slugs: list[str] = []
    for game in scheduled:
        away = _venue_team_code(game["away_team"]).lower()
        home = _venue_team_code(game["home_team"]).lower()
        base = f"nfl-{away}-{home}-{game['date']}"
        slugs.extend((base, f"aec-{base}"))
    return list(dict.fromkeys(slugs))


def _scope_pmus(
    client: PolymarketUSPublicClient,
    scheduled: list[dict],
    acquisition: PMUSAcquisition | None = None,
) -> tuple[list[dict], dict]:
    """Fetch only schedule-derived NFL slugs in one provider-supported query."""
    candidates = _pmus_slugs(scheduled)
    if len(candidates) > PMUS_NFL_MAX_SLUGS:
        return [], {
            "state": "PARTIAL",
            "reason": "schedule-derived PMUS slug ceiling exceeded",
            "candidate_slugs": len(candidates),
            "request_count": 0,
            "request_ceiling": client.max_requests_per_minute,
        }
    if not candidates:
        return [], {
            "state": "COMPLETE",
            "reason": "authoritative NFL slate is empty",
            "candidate_slugs": 0,
            "request_count": 0,
            "request_ceiling": client.max_requests_per_minute,
        }
    def request() -> list[dict]:
        rows = _scoped_call(
            client.markets_page,
            limit=len(candidates),
            offset=0,
            slugs=candidates,
            retry_transport_errors=False,
        )
        unexpected = [
            row for row in rows if str(row.get("slug") or "") not in candidates
        ]
        if unexpected and acquisition is not None:
            raise ValueError("PMUS response escaped exact NFL slug scope")
        return rows

    requests_before = acquisition.metrics["discovery_requests"] if acquisition else 0
    rows = (
        acquisition.discover(
            f"NFL:{scheduled[0]['date']}:{'|'.join(candidates)}", request
        )
        if acquisition
        else request()
    )
    unexpected = [row for row in rows if str(row.get("slug") or "") not in candidates]
    nfl_rows = [
        row
        for row in rows
        if str(row.get("slug") or "") in candidates
        and row.get("active")
        and not row.get("closed")
        and _is_nfl(row, row.get("raw", {}))
    ]
    return nfl_rows, {
        "state": "PARTIAL" if unexpected else "COMPLETE",
        "reason": (
            "PMUS response escaped exact slug scope"
            if unexpected
            else "exact schedule-derived PMUS slug set queried"
        ),
        "candidate_slugs": len(candidates),
        "markets_returned": len(rows),
        "unique_markets": len({str(row.get("id") or row.get("slug") or "") for row in rows}),
        "unexpected_markets": len(unexpected),
        "request_count": (
            acquisition.metrics["discovery_requests"] - requests_before
            if acquisition
            else 1
        ),
        "request_ceiling": client.max_requests_per_minute,
    }


def _kalshi_event_ticker(game: dict) -> str:
    date = datetime.fromisoformat(game["date"]).strftime("%y%b%d").upper()
    away = _venue_team_code(game["away_team"])
    home = _venue_team_code(game["home_team"])
    return f"{KALSHI_NFL_SERIES_TICKER}-{date}{away}{home}"


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


def _nfl_economic_position(play, mapping) -> tuple[str, str] | None:
    """Bind a scored side to one authoritative game/team economic position."""
    selected_team = economic_team_for_side(mapping, play.side)
    if selected_team is None or mapping.game is None:
        return None
    selected_label = NFL_TEAM_NAMES.get(selected_team)
    if selected_label is None:
        return None
    return f"NFL:{mapping.game.game_id}:{selected_team}", selected_label


def _buy_candidate_rank(candidate) -> tuple:
    play, _market, _mapping, _detected_at, _economic_key, _selected_team = candidate
    return (
        float("inf") if play.executable_price is None else float(play.executable_price),
        -(float("-inf") if play.edge_points is None else float(play.edge_points)),
        -float(play.executable_size or 0),
        str(play.venue),
        str(play.market_id),
        str(play.side),
    )


def _buy_candidate_publication_eligible(
    candidate,
    *,
    scheduled_game_ids: set[str],
    blocked_game_ids: set[str],
    now: datetime,
) -> bool:
    """Fail closed unless this economic BUY is independently safe to publish."""
    play, market, mapping, _detected_at, economic_key, selected_team = candidate
    game = getattr(mapping, "game", None)
    if (
        play.suggested_action != Action.BUY
        or getattr(mapping, "status", None) != "MAPPED_GAME_WINNER"
        or game is None
        or game.game_id not in scheduled_game_ids
        or game.game_id in blocked_game_ids
        or getattr(play, "demo", None) is not False
        or getattr(play, "data_freshness", None) != "FRESH"
        or getattr(play, "status", None) != "CURRENT"
        or tuple(getattr(getattr(play, "verdict", None), "failed_gates", ("missing",)))
        or getattr(play, "evidence", None) is None
    ):
        return False
    if _nfl_economic_position(play, mapping) != (economic_key, selected_team):
        return False
    if (
        getattr(market, "venue_market_id", None) != play.market_id
        or getattr(market, "venue", None) != play.venue
        or getattr(market, "status", None) != "OPEN"
    ):
        return False
    numeric_checks = (
        (getattr(play, "executable_price", None), 0, 1, True),
        (getattr(play, "model_probability", None), 0, 1, True),
        (getattr(play, "fees_estimate", None), 0, math.inf, False),
        (getattr(play, "edge_points", None), VALIDATION_ECE * 100, math.inf, True),
        (getattr(play, "executable_size", None), 0, math.inf, True),
        (getattr(play, "expected_value", None), 0, math.inf, True),
        (getattr(play, "expected_return", None), 0.05, math.inf, False),
    )
    for value, lower, upper, strict_lower in numeric_checks:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not (lower < value if strict_lower else lower <= value)
            or not value < upper
        ):
            return False
    updated_at = timestamp(getattr(play, "updated_at", None))
    expires_at = timestamp(getattr(play, "expires_at", None))
    kickoff = timestamp(getattr(game, "kickoff", None))
    return bool(
        updated_at
        and expires_at
        and kickoff
        and updated_at <= now < expires_at
        and now < kickoff
    )


def _publication_candidates(
    candidates,
    *,
    scheduled_game_ids: set[str],
    blocked_game_ids: set[str],
    now: datetime,
):
    """Choose one eligible contract for each independently safe economic BUY."""
    grouped = {}
    for candidate in candidates:
        if _buy_candidate_publication_eligible(
            candidate,
            scheduled_game_ids=scheduled_game_ids,
            blocked_game_ids=blocked_game_ids,
            now=now,
        ):
            grouped.setdefault(candidate[4], []).append(candidate)
    return [min(equivalents, key=_buy_candidate_rank) for equivalents in grouped.values()]


def _dispatch_buy_alert(
    dispatcher,
    play,
    market,
    mapping,
    detected_at,
    *,
    economic_key=None,
    selected_side=None,
):
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
        game_start=mapping.game.kickoff if mapping.game else None,
        economic_key=economic_key,
        selected_side=selected_side,
    )


def _dispatch_eligible_buy_alerts(dispatcher, candidates) -> dict[str, int]:
    """Dispatch one already-validated canonical BUY per economic position."""
    summary = {"sent": 0, "deduplicated": 0, "failed": 0, "withheld": 0}
    for candidate in candidates:
        play, market, mapping, detected_at, economic_key, selected_team = candidate
        try:
            result = _dispatch_buy_alert(
                dispatcher,
                play,
                market,
                mapping,
                detected_at,
                economic_key=economic_key,
                selected_side=selected_team,
            )
        except Exception:
            summary["failed"] += 1
            continue
        if result is None:
            continue
        if result["deduplicated"]:
            summary["deduplicated"] += 1
        elif result["status"] == "SENT":
            summary["sent"] += 1
        elif result["status"] in {"FAILED", "UNKNOWN", "PENDING"}:
            summary["failed"] += 1
    return summary


def _text(row: dict) -> str:
    return json.dumps(row, sort_keys=True, default=str).lower()


def _is_nfl(row: dict, *parents: dict) -> bool:
    """Scope using venue metadata/ticker first, then a narrow text fallback."""
    explicit = " ".join(_text(parent) for parent in parents)
    explicit += " " + " ".join(str(row.get(k) or "") for k in ("category", "sport", "league", "series_ticker", "event_ticker", "marketType", "sportsMarketTypeV2"))
    return bool(re.search(r"\bnfl\b|kx?nfl", explicit, re.I) or re.search(r"\bnfl\b", _text(row), re.I))


def _scoring_failure(exc: Exception) -> tuple[str, str]:
    """Return a compact redacted diagnostic without reflecting venue HTML."""
    seen: set[int] = set()
    current: BaseException | None = exc
    rate_limited = False
    cloudflare_1015 = False
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        text = " ".join(
            (
                str(current),
                str(getattr(current, "body", "")),
                str(getattr(current, "status_code", "")),
            )
        ).lower()
        cloudflare_1015 = cloudflare_1015 or "1015" in text
        rate_limited = rate_limited or (
            isinstance(current, PolymarketUSRateLimit)
            or getattr(current, "status_code", None) == 429
            or "rate limit" in text
            or "rate-limit" in text
            or cloudflare_1015
        )
        current = current.__cause__ or current.__context__
    if rate_limited:
        suffix = " (Cloudflare 1015)" if cloudflare_1015 else ""
        return "RATE_LIMITED", f"Polymarket US public API rate limited{suffix}"
    reason = " ".join(redact_sensitive(exc).split())[:240]
    return type(exc).__name__, reason or "book or scoring failed"


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


def _scope_kalshi(client: KalshiPublicClient, scheduled: list[dict]) -> tuple[list[dict], dict]:
    """Fetch the NFL series directly, then retain only schedule-backed events."""
    series_row = {
        "ticker": KALSHI_NFL_SERIES_TICKER,
        "sport": "NFL",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }
    request_count = 0

    def bounded_call(function, **kwargs):
        nonlocal request_count
        if request_count >= KALSHI_NFL_DISCOVERY_MAX_REQUESTS:
            raise RuntimeError("Kalshi NFL discovery request ceiling reached")
        request_count += 1
        return _scoped_call(function, **kwargs)

    try:
        markets, market_cov = paginate_collection(
            lambda *, limit, cursor: bounded_call(
                client.nfl_markets_page,
                limit=limit,
                cursor=cursor,
            ),
            key="markets",
            max_pages=KALSHI_NFL_DISCOVERY_MAX_REQUESTS,
        )
    except Exception as exc:
        return [], {
            "state": "PARTIAL",
            "series_ticker": KALSHI_NFL_SERIES_TICKER,
            "events_observed": 0,
            "markets_returned": 0,
            "unique_markets": 0,
            "request_count": request_count,
            "request_ceiling": KALSHI_NFL_DISCOVERY_MAX_REQUESTS,
            "failed_scopes": [
                {"scope": KALSHI_NFL_SERIES_TICKER, "stage": "markets", "error": type(exc).__name__}
            ],
        }

    slate_by_event = {_kalshi_event_ticker(game): game for game in scheduled}
    rows: list[dict] = []
    failures: list[dict] = []
    for market in markets:
        event_ticker = str(market.get("event_ticker") or "").upper()
        market_ticker = str(market.get("ticker") or "").upper()
        if not event_ticker or not market_ticker.startswith(f"{event_ticker}-"):
            failures.append({
                "scope": market_ticker or KALSHI_NFL_SERIES_TICKER,
                "stage": "market_event_identity",
                "error": "TickerMismatch",
            })
            continue
        game = slate_by_event.get(event_ticker)
        if game is None:
            continue
        event = {
            "ticker": event_ticker,
            "event_ticker": event_ticker,
            "series_ticker": KALSHI_NFL_SERIES_TICKER,
            "title": f"{game['away_team']} vs {game['home_team']} NFL game",
            "sport": "NFL",
            "away_team": game["away_team"],
            "home_team": game["home_team"],
            "scheduled_start": game["start_time"],
        }
        rows.append({
            **market,
            "away_team": market.get("away_team") or game["away_team"],
            "home_team": market.get("home_team") or game["home_team"],
            "scheduled_start": market.get("scheduled_start") or game["start_time"],
            "_discovery_event": event,
            "_discovery_series": series_row,
        })
    unique = {}
    for row in rows:
        key = str(row.get("ticker") or row.get("id") or "")
        if key:
            unique.setdefault(key, row)
    state = "PARTIAL" if failures else market_cov.state.value
    return list(unique.values()), {
        "state": state,
        "series_ticker": KALSHI_NFL_SERIES_TICKER,
        "market_pages": market_cov.pages,
        "events_observed": len({row.get("event_ticker") for row in rows}),
        "markets_returned": len(markets),
        "slate_markets": len(rows),
        "unique_markets": len(unique),
        "request_count": request_count,
        "request_ceiling": KALSHI_NFL_DISCOVERY_MAX_REQUESTS,
        "failed_scopes": failures,
        "market_coverage_reason": market_cov.reason,
    }


def _scan(*, pmus_acquisition: PMUSAcquisition | None = None) -> dict:
    games = fetch_games()
    discovery_at = utcnow()
    scheduled_for_discovery = _upcoming_slate(games, discovery_at)
    result = {"read_only": True, "orders": 0, "alerts": 0, "published": 0, "prospective_captured": 0, "validation_ece": VALIDATION_ECE, "venues": {}}
    prospective_store = TrackRecord(PROSPECTIVE_DB)
    alert_dispatcher = AlertDispatcher(
        AlertDeliveryStore(default_inbox_store().path)
    )
    pmus_rows: list[dict] = []
    pmus_discovery_complete = False
    kalshi_discovery_complete = False
    acquisition = pmus_acquisition or PMUSAcquisition("NFL")
    pmus = PolymarketUSPublicClient()
    try:
        try:
            pmus_rows, pmus_cov = _scope_pmus(
                pmus, scheduled_for_discovery, acquisition
            )
            result["venues"]["PMUS"] = {
                "coverage": pmus_cov,
                "universe_rows": len(pmus_rows),
                "nfl_rows": len(pmus_rows),
            }
            pmus_discovery_complete = pmus_cov.get("state") == "COMPLETE"
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
        kalshi = KalshiPublicClient()
        try:
            kalshi_rows, kalshi_cov = _scope_kalshi(kalshi, scheduled_for_discovery)
            result["venues"]["KALSHI"] = {"coverage": kalshi_cov, "nfl_rows": len(kalshi_rows)}
            kalshi_discovery_complete = kalshi_cov.get("state") == "COMPLETE"
        except Exception as exc:
            kalshi_rows, kalshi_cov = [], {"state": "PARTIAL", "failed_scopes": [{"stage": "series", "error": type(exc).__name__}]}
            result["venues"]["KALSHI"] = {"coverage": kalshi_cov, "nfl_rows": 0}
        statuses = Counter()
        rejection_reasons = Counter()
        rows_out = []
        scored_plays = []
        buy_candidates = []
        scored_rows_by_play_id = {}
        economic_keys_by_play_id = {}
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
                support_reason = market_support_reason(market)
                if support_reason != "SUPPORTED":
                    rejection_reasons[support_reason] += 1
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
                            evidence = NFLEvidenceProvider(lambda: games).assess(market)
                            if evidence is None:
                                statuses["EVIDENCE_MISSING"] += 1
                                data_unavailable_game_ids.add(mapping.game.game_id)
                                rows_out.append(row)
                                continue
                            acquired = acquisition.book(
                                raw["slug"],
                                lambda raw=raw: pmus.book(raw["slug"]),
                                fair_probability=evidence.fair_probability,
                                calibration_edge=VALIDATION_ECE,
                            )
                            if acquired.book is None or acquired.observed_at is None:
                                statuses["BOOK_REQUEST_AVOIDED"] += 1
                                data_unavailable_game_ids.add(mapping.game.game_id)
                                row["scoring_error"] = "ACQUISITION_AVOIDED"
                                row["scoring_error_reason"] = acquired.avoided_reason
                                rows_out.append(row)
                                continue
                            market = attach_fees(
                                normalize_pmus(
                                    raw, acquired.book, acquired.observed_at
                                ),
                                utcnow(),
                            )
                        else:
                            # Kalshi discovery rows are normalized with their public book
                            # below when the venue exposes one; failures stay explicit.
                            book = kalshi.book(raw["ticker"])
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
                                scored_plays.append(play)
                                statuses[f"SCORED_{play.suggested_action}"] += 1
                                economic_position = _nfl_economic_position(play, mapping)
                                if economic_position is None:
                                    raise ValueError(
                                        "scored NFL side lacks authoritative economic identity"
                                    )
                                economic_key, selected_team = economic_position
                                economic_keys_by_play_id[play.id] = economic_key
                                if play.suggested_action == Action.BUY:
                                    buy_candidates.append(
                                        (
                                            play,
                                            market,
                                            mapping,
                                            decision_at,
                                            economic_key,
                                            selected_team,
                                        )
                                    )
                                scored = {"venue": venue, "market": market.title, "market_id": market.venue_market_id, "game_id": mapping.game.game_id, "side": side.value, "game_start": mapping.game.kickoff, "updated_at": getattr(play, "updated_at", None), "expires_at": getattr(play, "expires_at", None), "data_freshness": getattr(play, "data_freshness", None), "status": getattr(play, "status", None), "nfl_v1_probability": play.model_probability, "executable_price": play.executable_price, "raw_edge": play.edge_points, "safety_margin": (play.edge_points / 100 - VALIDATION_ECE) if play.edge_points is not None else None, "fee": play.fees_estimate, "net_ev_25": play.expected_value, "expected_return": getattr(play, "expected_return", None), "net_ev_50": None, "net_ev_100": None, "liquidity": play.executable_size, "failed_gates": list(play.verdict.failed_gates), "verdict": play.suggested_action.value, "mapping_status": mapping.status, "acquisition_status": "ACQUIRED", "publication_eligible": False}
                                scored["economic_key"] = economic_key
                                scored["economic_team"] = economic_key.rsplit(":", 1)[-1]
                                scored["selected_team"] = selected_team
                                for index, key in ((2, "net_ev_50"), (3, "net_ev_100")):
                                    example = play.retail_examples[index]
                                    scored[key] = (play.model_probability * example.estimated_payout_if_correct - example.total_cost) if play.model_probability is not None and example.available and example.total_cost is not None else None
                                rows_out.append(scored)
                                scored_rows_by_play_id[play.id] = scored
                    except Exception as exc:
                        statuses["BOOK_OR_SCORING_ERROR"] += 1
                        data_unavailable_game_ids.add(mapping.game.game_id)
                        row["scoring_error"], row["scoring_error_reason"] = _scoring_failure(exc)
                rows_out.append(row)
    finally:
        pmus.close()
    result["pmus_acquisition"] = acquisition.diagnostics()
    result["slate"] = reconcile_slate(
        scheduled_for_discovery,
        rows_out,
        discovery_complete=pmus_discovery_complete and kalshi_discovery_complete,
        data_unavailable_game_ids=data_unavailable_game_ids,
        mapping_failure_game_ids=mapping_failure_game_ids,
    )
    publication_at = utcnow()
    blocked_game_ids = data_unavailable_game_ids | mapping_failure_game_ids
    publication_candidates = _publication_candidates(
        buy_candidates,
        scheduled_game_ids={game["game_id"] for game in scheduled_for_discovery},
        blocked_game_ids=blocked_game_ids,
        now=publication_at,
    )
    for play, _market, _mapping, _detected_at, _key, _team in publication_candidates:
        scored_rows_by_play_id[play.id]["publication_eligible"] = True
    alert_summary = _dispatch_eligible_buy_alerts(
        alert_dispatcher, publication_candidates
    )
    alert_summary["withheld"] = sum(
        not _buy_candidate_publication_eligible(
            candidate,
            scheduled_game_ids={game["game_id"] for game in scheduled_for_discovery},
            blocked_game_ids=blocked_game_ids,
            now=publication_at,
        )
        for candidate in buy_candidates
    )
    result["alerts"] = alert_summary["sent"]
    statuses["ALERT_DEDUPLICATED"] += alert_summary["deduplicated"]
    statuses["ALERT_ERROR"] += alert_summary["failed"]
    statuses["BUY_ALERT_WITHHELD_INELIGIBLE"] += alert_summary["withheld"]
    lifecycle_at = publication_at
    result["buy_lifecycle"] = reconcile_active_buy_alerts(
        alert_dispatcher,
        scored_plays,
        sport="NFL",
        detected_at=lifecycle_at.isoformat(),
        economic_key_for_play=lambda play: economic_keys_by_play_id.get(play.id),
    )
    result["summary"] = {"nfl_markets_discovered": {"PMUS": len(pmus_rows), "KALSHI": len(kalshi_rows)}, "status_counts": dict(statuses), "rejection_counts": dict(rejection_reasons), "rows": rows_out}
    return result


if __name__ == "__main__":
    print(json.dumps(_scan(), default=lambda value: value.value if hasattr(value, "value") else value, allow_nan=False))
