#!/usr/bin/env python3
"""Read-only Polymarket microstructure observer for an explicit event window.

This script only reads public market metadata and writes observations to its
own append-only table. It has no inference, model, portfolio, or order path.
"""
from __future__ import annotations

import argparse
import hashlib
import signal
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "migrations" / "012_event_microstructure_snapshots.sql"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.polymarket_adapter import PolymarketAdapter  # noqa: E402
from swarm_edge_runtime import RUNTIME_PATHS  # noqa: E402


@dataclass(frozen=True)
class MarketTarget:
    slug: str | None = None
    market_id: str | None = None


def parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid UTC timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp must include a timezone, e.g. Z")
    return parsed.astimezone(timezone.utc)


def validate_window(start: datetime, end: datetime, interval: int) -> None:
    if start >= end:
        raise ValueError("start must be before end")
    if interval < 30:
        raise ValueError("interval must be at least 30 seconds")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def observation_from_market(
    market: dict[str, Any], *, event_label: str, observed_at: str,
    requested_slug: str | None = None, requested_market_id: str | None = None,
    fetch_status: str = "ok", fetch_latency_ms: float | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    market_id = str(market.get("id") or requested_market_id or requested_slug or "")
    slug = str(market.get("slug") or requested_slug or "")
    bid, ask = _number(market.get("bestBid")), _number(market.get("bestAsk"))
    identity = f"{event_label}|{observed_at}|{market_id}|{slug}"
    return {
        "observation_key": hashlib.sha256(identity.encode()).hexdigest(),
        "event_label": event_label,
        "observation_timestamp_utc": observed_at,
        "market_id": market_id,
        "slug": slug,
        "venue": "polymarket",
        "best_bid": bid,
        "best_ask": ask,
        "midpoint": (bid + ask) / 2 if bid is not None and ask is not None else None,
        "last_trade_price": _number(market.get("lastTradePrice")),
        "spread": ask - bid if bid is not None and ask is not None else None,
        "liquidity": _number(market.get("liquidity")),
        "volume": _number(market.get("volume")),
        "source_updated_at": _timestamp(market.get("updatedAt")),
        "fetch_status": fetch_status,
        "fetch_latency_ms": fetch_latency_ms,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }


def persist_observations(conn: sqlite3.Connection, rows: Iterable[dict[str, Any]]) -> dict[str, int]:
    rows = list(rows)
    inserted = 0
    for row in rows:
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO event_microstructure_snapshots
                (observation_key,event_label,observation_timestamp_utc,market_id,slug,venue,
                 best_bid,best_ask,midpoint,last_trade_price,spread,liquidity,volume,
                 source_updated_at,fetch_status,fetch_latency_ms,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                tuple(row[field] for field in (
                    "observation_key", "event_label", "observation_timestamp_utc", "market_id",
                    "slug", "venue", "best_bid", "best_ask", "midpoint", "last_trade_price",
                    "spread", "liquidity", "volume", "source_updated_at", "fetch_status",
                    "fetch_latency_ms", "created_at",
                )),
            )
            inserted += int(cursor.rowcount == 1)
        except sqlite3.Error:
            conn.rollback()
            continue
    conn.commit()
    return {"prepared": len(rows), "inserted": inserted, "duplicates": len(rows) - inserted}


def observe_once(
    conn: sqlite3.Connection, adapter: PolymarketAdapter, targets: Sequence[MarketTarget],
    *, event_label: str, observed_at: str, clock: Callable[[], float] = time.monotonic,
    dry_run: bool = False,
) -> dict[str, int]:
    rows: list[dict[str, Any]] = []
    for target in targets:
        started = clock()
        try:
            market = adapter.get_market(target.slug) if target.slug else adapter.get_market_by_id(target.market_id)
            latency = round((clock() - started) * 1000, 3)
            rows.append(observation_from_market(
                market, event_label=event_label, observed_at=observed_at,
                requested_slug=target.slug, requested_market_id=target.market_id,
                fetch_latency_ms=latency,
            ))
        except Exception as exc:  # one bad market must not end the window
            latency = round((clock() - started) * 1000, 3)
            fallback = {"id": target.market_id, "slug": target.slug}
            rows.append(observation_from_market(
                fallback, event_label=event_label, observed_at=observed_at,
                requested_slug=target.slug, requested_market_id=target.market_id,
                fetch_status=f"error:{type(exc).__name__}", fetch_latency_ms=latency,
            ))
    if dry_run:
        return {"prepared": len(rows), "inserted": 0, "duplicates": 0}
    return persist_observations(conn, rows)


def run_observer(
    conn: sqlite3.Connection, adapter: PolymarketAdapter, targets: Sequence[MarketTarget],
    *, event_label: str, start: datetime, end: datetime, interval: int,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    sleep: Callable[[float], None] = time.sleep, stop: Callable[[], bool] = lambda: False,
    dry_run: bool = False, max_cycles: int | None = None,
) -> int:
    validate_window(start, end, interval)
    cycles = 0
    while not stop():
        current = now().astimezone(timezone.utc)
        if current >= end or (max_cycles is not None and cycles >= max_cycles):
            break
        if current < start:
            sleep(min(interval, (start - current).total_seconds()))
            continue
        observed_at = current.isoformat()
        result = observe_once(conn, adapter, targets, event_label=event_label, observed_at=observed_at, dry_run=dry_run)
        print(f"{observed_at} event={event_label} markets={result['prepared']} inserted={result['inserted']}", flush=True)
        cycles += 1
        if max_cycles is not None and cycles >= max_cycles:
            break
        remaining = (end - now().astimezone(timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        sleep(min(interval, remaining))
    return cycles


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event-label", required=True)
    parser.add_argument("--start", required=True, type=parse_utc)
    parser.add_argument("--end", required=True, type=parse_utc)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--market-slug", action="append", dest="market_slugs", default=[])
    parser.add_argument("--market-id", action="append", dest="market_ids", default=[])
    parser.add_argument("--db", type=Path, default=RUNTIME_PATHS.db_path)
    parser.add_argument("--dry-run", action="store_true", help="fetch and print; do not persist")
    parser.add_argument("--short-cycles", type=int, metavar="N", help="stop after N polling cycles")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        validate_window(args.start, args.end, args.interval)
        if not args.market_slugs and not args.market_ids:
            raise ValueError("at least one --market-slug or --market-id is required")
        if args.short_cycles is not None and args.short_cycles < 1:
            raise ValueError("short-cycles must be positive")
    except ValueError as exc:
        parser.error(str(exc))
    targets = [MarketTarget(slug=value.strip()) for value in args.market_slugs]
    targets.extend(MarketTarget(market_id=value.strip()) for value in args.market_ids)
    if any(not (target.slug or target.market_id) for target in targets):
        parser.error("market targets cannot be empty")
    conn = sqlite3.connect(args.db)
    try:
        if not args.dry_run:
            ensure_schema(conn)
        stop_requested = {"value": False}
        signal.signal(signal.SIGINT, lambda *_: stop_requested.__setitem__("value", True))
        try:
            run_observer(
                conn, PolymarketAdapter(), targets, event_label=args.event_label, start=args.start,
                end=args.end, interval=args.interval, stop=lambda: stop_requested["value"],
                dry_run=args.dry_run, max_cycles=args.short_cycles,
            )
        except KeyboardInterrupt:
            print("observer stopped", flush=True)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
