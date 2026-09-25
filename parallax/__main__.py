from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Event, Thread

from .alerts import dispatch_scored_buy, reconcile_active_buy_alerts
from .api import server
from .demo import demo_inputs
from .entitlements import Plan
from .mlb import selected_team_for_moneyline
from .models import Action, utcnow
from .service import PlayService
from .sources import collect_markets
from .slate import reconcile_slate
from .track_record import TrackRecord


def make_service(db: str, *, demo: bool = False, limit: int = 6) -> PlayService:
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    prospective = None if demo else TrackRecord("data/parallax-commercial/prospective.sqlite")
    service = PlayService(
        TrackRecord(path),
        prospective_store=prospective,
        capture_only=not demo,
    )
    refresh(service, demo=demo, limit=limit)
    return service


def refresh(service: PlayService, *, demo: bool, limit: int):
    if demo:
        markets, evidence = demo_inputs()
        service.replace_inputs(markets, evidence, mode="demo")
    else:
        markets, collection = collect_markets(limit)
        service.replace_inputs(markets, collection=collection)


def _market_game_start(market) -> str | None:
    metadata = market.original_metadata.get("market", {})
    if not isinstance(metadata, dict):
        return None
    mlb = metadata.get("mlb")
    if isinstance(mlb, dict) and mlb.get("start_time"):
        return str(mlb["start_time"])
    for key in ("gameStartTime", "game_start_time", "startTime", "start_time"):
        if metadata.get(key):
            return str(metadata[key])
    return None


def _mlb_economic_position(
    service: PlayService,
    play,
    market,
) -> tuple[str | None, str | None]:
    """Map equivalent MLB binary contracts to one game/team economic position."""
    collection = service.collection if isinstance(getattr(service, "collection", None), dict) else {}
    market_game_ids = collection.get("_market_game_ids")
    if not isinstance(market_game_ids, dict):
        return None, None
    venue = play.venue.value if hasattr(play.venue, "value") else str(play.venue)
    game_id = market_game_ids.get(f"{venue}:{play.market_id}")
    selected_team = selected_team_for_moneyline(market, play.side)
    if not game_id or not selected_team:
        return None, selected_team
    team_key = "".join(ch for ch in selected_team.casefold() if ch.isalnum())
    if not team_key:
        return None, selected_team
    return f"MLB:{game_id}:{team_key}", selected_team


def _buy_candidate_rank(candidate) -> tuple:
    play, _market, _economic_key, _selected_side = candidate
    price = play.executable_price
    edge = play.edge_points
    size = play.executable_size
    return (
        float("inf") if price is None else float(price),
        -(float("-inf") if edge is None else float(edge)),
        -float(size or 0),
        str(play.venue),
        str(play.market_id),
        str(play.side),
    )


def dispatch_scan_buy_alerts(service: PlayService, *, sport: str) -> dict[str, int]:
    """Dispatch at most one immediate alert per economic BUY position."""
    markets = {
        (market.venue, market.venue_market_id): market
        for market in service.markets
    }
    summary = {"sent": 0, "deduplicated": 0, "failed": 0}
    grouped: dict[str, list[tuple]] = {}
    for play in service._plays():
        if play.demo or play.suggested_action != Action.BUY:
            continue
        market = markets.get((play.venue, play.market_id))
        if market is None:
            summary["failed"] += 1
            continue
        economic_key = None
        selected_side = None
        if sport.upper() == "MLB":
            economic_key, selected_side = _mlb_economic_position(service, play, market)
        grouping_key = economic_key or (
            f"{sport.upper()}:{play.venue}:{play.market_id}:{play.side}"
        )
        grouped.setdefault(grouping_key, []).append(
            (play, market, economic_key, selected_side)
        )

    for candidates in grouped.values():
        play, market, economic_key, selected_side = min(
            candidates, key=_buy_candidate_rank
        )
        try:
            result = dispatch_scored_buy(
                service.alert_dispatcher,
                play,
                market,
                sport=sport,
                matchup=market.title,
                detected_at=play.updated_at,
                game_start=_market_game_start(market),
                economic_key=economic_key,
                selected_side=selected_side,
            )
        except Exception:  # Alert delivery must never fail the scan.
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


def mlb_slate_report(service: PlayService) -> dict:
    """Reconcile today's authoritative MLB schedule against scored markets."""
    collection = service.collection if isinstance(service.collection, dict) else {}
    schedule_state = str(collection.get("_slate_schedule_state") or "DATA_UNAVAILABLE")
    schedule = collection.get("_slate_schedule")
    if schedule_state != "COMPLETE" or not isinstance(schedule, list):
        return {
            "schedule_state": schedule_state,
            "expected_games": None,
            "accounted_games": 0,
            "all_games_accounted": False,
            "market_data_complete": False,
            "status_counts": {"DATA_UNAVAILABLE": 1},
            "dates": [],
        }

    market_game_ids = collection.get("_market_game_ids")
    if not isinstance(market_game_ids, dict):
        market_game_ids = {}
    observations = []
    for play in service._plays():
        venue = play.venue.value if hasattr(play.venue, "value") else str(play.venue)
        game_id = market_game_ids.get(f"{venue}:{play.market_id}")
        if game_id:
            action = (
                play.suggested_action.value
                if hasattr(play.suggested_action, "value")
                else str(play.suggested_action)
            )
            observations.append({"game_id": game_id, "verdict": action})

    report = reconcile_slate(
        schedule,
        observations,
        discovery_complete=bool(collection.get("_slate_discovery_complete")),
        data_unavailable_game_ids=collection.get(
            "_slate_data_unavailable_game_ids", ()
        ),
    )
    report["schedule_state"] = schedule_state
    return report


def reconcile_scan_buy_lifecycle(service: PlayService, *, sport: str) -> dict[str, int]:
    markets = {
        (market.venue, market.venue_market_id): market
        for market in getattr(service, "markets", ())
    }

    def economic_key_for_play(play):
        if sport.upper() != "MLB":
            return None
        market = markets.get((play.venue, play.market_id))
        if market is None:
            return None
        key, _selected_side = _mlb_economic_position(service, play, market)
        return key

    return reconcile_active_buy_alerts(
        service.alert_dispatcher,
        service._plays(),
        sport=sport,
        detected_at=utcnow().isoformat(),
        economic_key_for_play=economic_key_for_play,
    )


def start_refresh_loop(service: PlayService, *, demo: bool, limit: int) -> Event:
    stop = Event()

    def refresh_loop():
        while not stop.wait(30):
            try:
                refresh(service, demo=demo, limit=limit)
            except Exception:  # noqa: BLE001 - keep old quotes aging after failure
                service.failures += 1

    Thread(target=refresh_loop, daemon=True).start()
    return stop


def main():
    parser = argparse.ArgumentParser(
        description="PARALLAX public market intelligence; no execution"
    )
    parser.add_argument("command", choices=("scan", "serve"))
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Clearly marked synthetic fixtures; never published",
    )
    parser.add_argument(
        "--limit", type=int, default=6, help="Bounded market sample per venue, 1–100"
    )
    parser.add_argument("--db", default="data/parallax-commercial/publications.sqlite")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--summary", action="store_true", help="Compact smoke output")
    args = parser.parse_args()
    service = make_service(args.db, demo=args.demo, limit=args.limit)
    if args.command == "scan":
        buy_alerts = (
            {"sent": 0, "deduplicated": 0, "failed": 0}
            if args.demo
            else dispatch_scan_buy_alerts(service, sport="MLB")
        )
        payload = {"health": service.health(), "buy_alerts": buy_alerts}
        if not args.demo:
            payload["buy_lifecycle"] = reconcile_scan_buy_lifecycle(service, sport="MLB")
            payload["slate"] = mlb_slate_report(service)
        payload["plays"] = service.plays(Plan.PRO)
        payload["signals"] = service.signals(Plan.PRO)
        if args.summary:
            payload["plays"]["items"] = [
                {
                    k: p[k]
                    for k in (
                        "venue",
                        "market_id",
                        "side",
                        "current_price",
                        "suggested_action",
                        "data_freshness",
                    )
                }
                for p in payload["plays"]["items"]
            ]
        print(json.dumps(payload, allow_nan=False))
        return
    stop = start_refresh_loop(service, demo=args.demo, limit=args.limit)
    api = server(service, args.port, (lambda headers: Plan.PRO) if args.demo else None)
    print(
        f"PARALLAX {'SYNTHETIC DEMO' if args.demo else 'read-only live'} API: http://127.0.0.1:{args.port}",
        flush=True,
    )
    try:
        api.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        api.server_close()


if __name__ == "__main__":
    main()
