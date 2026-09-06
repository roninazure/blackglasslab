from __future__ import annotations

import argparse
import json
from pathlib import Path
from threading import Event, Thread

from .api import server
from .demo import demo_inputs
from .entitlements import Plan
from .service import PlayService
from .sources import collect_markets
from .track_record import TrackRecord


def make_service(db: str, *, demo: bool = False, limit: int = 6) -> PlayService:
    path = Path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    service = PlayService(TrackRecord(path))
    refresh(service, demo=demo, limit=limit)
    return service


def refresh(service: PlayService, *, demo: bool, limit: int):
    if demo:
        markets, evidence = demo_inputs()
        service.replace_inputs(markets, evidence, mode="demo")
    else:
        markets, collection = collect_markets(limit)
        service.replace_inputs(markets, collection=collection)


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
        payload = {"health": service.health()}
        payload["plays"] = service.plays(Plan.PRO)
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
