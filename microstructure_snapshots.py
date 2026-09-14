"""Append-only telemetry for already-observed Polymarket market responses."""
from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
MIGRATION_PATH = ROOT / "migrations" / "011_market_microstructure_snapshots.sql"


def ensure_microstructure_schema(conn: sqlite3.Connection) -> None:
    """Install the additive telemetry table on a writable connection."""
    conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
    conn.commit()


def _float_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso_timestamp(value: Any) -> str | None:
    parsed = _timestamp(value)
    return parsed.isoformat() if parsed is not None else None


def snapshot_from_market(
    market: dict[str, Any],
    *,
    timestamp_utc: str,
    cycle_id: str | None,
    venue: str,
    slug: str,
) -> dict[str, Any]:
    """Normalize only values present in the existing Gamma market response."""
    bid = _float_or_none(market.get("bestBid"))
    ask = _float_or_none(market.get("bestAsk"))
    observed = _timestamp(timestamp_utc)
    resolution = _iso_timestamp(market.get("resolutionDate") or market.get("endDate"))
    remaining = (
        (_timestamp(resolution) - observed).total_seconds() / 3600.0
        if resolution is not None and observed is not None
        else None
    )
    identity = f"{cycle_id or timestamp_utc}|{venue}|{market.get('id') or slug}"
    return {
        "observation_key": hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        "timestamp_utc": timestamp_utc,
        "cycle_id": cycle_id,
        "venue": venue,
        "market_id": str(market.get("id") or slug),
        "slug": slug,
        "best_bid": bid,
        "best_ask": ask,
        "midpoint": (bid + ask) / 2.0 if bid is not None and ask is not None else None,
        "last_trade": _float_or_none(market.get("lastTradePrice")),
        "spread": ask - bid if bid is not None and ask is not None else None,
        # The normal inference path does not fetch a CLOB book.
        "bid_executable_depth_usd": None,
        "ask_executable_depth_usd": None,
        "liquidity": _float_or_none(market.get("liquidity")),
        "volume": _float_or_none(market.get("volume")),
        "resolution_timestamp_utc": resolution,
        "time_remaining_hours": remaining,
        "source": "polymarket_gamma_market_response",
        "source_updated_at_utc": _iso_timestamp(market.get("updatedAt")),
    }


def persist_snapshots(
    conn: sqlite3.Connection,
    snapshots: Iterable[dict[str, Any]],
) -> dict[str, int | str | None]:
    """Persist a cycle batch without allowing telemetry errors to escape."""
    rows = list(snapshots)
    result: dict[str, int | str | None] = {
        "prepared": len(rows), "inserted": 0, "duplicates": 0, "failed": 0,
        "error": None,
    }
    if not rows:
        return result
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            ("market_microstructure_snapshots",),
        ).fetchone()
        if table is None:
            result["failed"] = len(rows)
            result["error"] = "missing_table"
            return result
        conn.execute("SAVEPOINT microstructure_snapshot_write")
        cursor = conn.executemany(
            """INSERT OR IGNORE INTO market_microstructure_snapshots
            (observation_key,timestamp_utc,cycle_id,venue,market_id,slug,
             best_bid,best_ask,midpoint,last_trade,spread,
             bid_executable_depth_usd,ask_executable_depth_usd,liquidity,volume,
             resolution_timestamp_utc,time_remaining_hours,source,source_updated_at_utc)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [
                tuple(row.get(field) for field in (
                    "observation_key", "timestamp_utc", "cycle_id", "venue",
                    "market_id", "slug", "best_bid", "best_ask", "midpoint",
                    "last_trade", "spread", "bid_executable_depth_usd",
                    "ask_executable_depth_usd", "liquidity", "volume",
                    "resolution_timestamp_utc", "time_remaining_hours", "source",
                    "source_updated_at_utc",
                ))
                for row in rows
            ],
        )
        conn.execute("RELEASE SAVEPOINT microstructure_snapshot_write")
        conn.commit()
        inserted = int(cursor.rowcount if cursor.rowcount >= 0 else 0)
        result["inserted"] = inserted
        result["duplicates"] = len(rows) - inserted
    except Exception as exc:  # telemetry must never affect production decisions
        try:
            conn.execute("ROLLBACK TO SAVEPOINT microstructure_snapshot_write")
            conn.execute("RELEASE SAVEPOINT microstructure_snapshot_write")
        except sqlite3.Error:
            pass
        result["failed"] = len(rows)
        result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
    return result
