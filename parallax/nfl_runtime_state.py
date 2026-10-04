"""Crash-safe local runtime state for the high-frequency NFL scan.

Only public market identity metadata and provider availability diagnostics are
stored here.  Executable books are deliberately never persisted by this
module, so a restart cannot make an old quote look fresh.
"""

from __future__ import annotations

import json
import os
import tempfile
from math import ceil
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

KALSHI_DISCOVERY_INTERVAL_SECONDS = 60 * 60
KALSHI_REFRESH_MAX_MARKETS = 12
PMUS_STARTUP_COOLDOWN_SECONDS = 15 * 60
NFL_LOCAL_TIME = ZoneInfo("America/New_York")


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)
    directory_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _parse_utc(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def kalshi_kickoff_tier(row: Mapping[str, Any], now: datetime) -> str:
    """Classify a known winner from its authoritative scheduled kickoff."""
    event = row.get("_discovery_event") or {}
    kickoff = _parse_utc(event.get("scheduled_start") or row.get("scheduled_start"))
    if kickoff is None:
        return "UNKNOWN"
    if kickoff <= now:
        return "STARTED"
    return "HOT" if kickoff.astimezone(NFL_LOCAL_TIME).date() == now.astimezone(NFL_LOCAL_TIME).date() else "WARM"


class NFLRuntimeState:
    """Persist NFL discovery candidates and the PMUS startup circuit."""

    def __init__(
        self,
        state_dir: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.kalshi_path = self.state_dir / "nfl_kalshi_refresh.json"
        self.pmus_path = self.state_dir / "nfl_pmus_startup_cooldown.json"
        self.clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        now = self.clock()
        if now.tzinfo is None:
            raise ValueError("NFL runtime-state clock must be timezone-aware")
        return now.astimezone(UTC)

    @staticmethod
    def _read(path: Path) -> Mapping[str, Any] | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(value, Mapping):
            raise TypeError("runtime state must be a JSON object")
        return value

    def kalshi_snapshot(self) -> dict[str, Any]:
        """Return validated cache state; corruption safely forces discovery."""
        now = self._now()
        try:
            payload = self._read(self.kalshi_path)
            discovered_at = _parse_utc(payload.get("discovered_at")) if payload else None
            rows = payload.get("markets") if payload else []
            tickers = payload.get("refresh_tickers") if payload else []
            buys = payload.get("buy_tickers", []) if payload else []
            buy_cursor = payload.get("buy_cursor", 0) if payload else 0
            other_cursor = payload.get("other_cursor", 0) if payload else 0
            warm_cursor = payload.get("warm_cursor", 0) if payload else 0
            observed = payload.get("last_observed_at_by_ticker", {}) if payload else {}
            if (
                (
                    payload is not None
                    and payload.get("schema_version") != 2
                )
                or not isinstance(rows, list)
                or not all(
                    isinstance(row, dict)
                    and isinstance(row.get("ticker"), str)
                    and row["ticker"]
                    for row in rows
                )
                or not isinstance(tickers, list)
                or not all(isinstance(ticker, str) and ticker for ticker in tickers)
                or not isinstance(buys, list)
                or not all(isinstance(ticker, str) and ticker for ticker in buys)
                or (
                    payload is not None
                    and not isinstance(payload.get("discovery_complete"), bool)
                )
                or set(tickers) != {row["ticker"] for row in rows}
                or not set(buys) <= set(tickers)
                or not isinstance(buy_cursor, int)
                or buy_cursor < 0
                or not isinstance(other_cursor, int)
                or other_cursor < 0
                or not isinstance(warm_cursor, int)
                or warm_cursor < 0
                or not isinstance(observed, dict)
                or not set(observed) <= set(tickers)
                or any(not isinstance(ticker, str) or _parse_utc(value) is None for ticker, value in observed.items())
            ):
                raise ValueError("invalid Kalshi refresh state")
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return {
                "discovery_due": True,
                "discovered_at": None,
                "markets": [],
                "refresh_tickers": [],
                "discovery_complete": False,
                "buy_tickers": [],
                "buy_cursor": 0,
                "other_cursor": 0,
                "warm_cursor": 0,
                "last_observed_at_by_ticker": {},
                "state": "CORRUPT",
            }
        age = (now - discovered_at).total_seconds() if discovered_at else None
        return {
            "discovery_due": age is None or age < 0 or age >= KALSHI_DISCOVERY_INTERVAL_SECONDS,
            "discovered_at": discovered_at.isoformat() if discovered_at else None,
            "markets": list(rows or []),
            "refresh_tickers": list(dict.fromkeys(tickers or [])),
            "discovery_complete": bool(payload.get("discovery_complete", False)) if payload else False,
            "buy_tickers": list(dict.fromkeys(buys)),
            "buy_cursor": buy_cursor,
            "other_cursor": other_cursor,
            "warm_cursor": warm_cursor,
            "last_observed_at_by_ticker": dict(observed),
            "state": "READY" if payload else "MISSING",
        }

    def kalshi_refresh_rows(self, snapshot: Mapping[str, Any]) -> tuple[list[dict], dict]:
        """Pin today's BUYs, fairly rotate today's other books, then future books."""
        now = self._now()
        tickers = set(snapshot.get("refresh_tickers") or [])
        hot: list[dict] = []
        warm: list[dict] = []
        started = 0
        unknown_kickoff = 0
        for row in snapshot.get("markets") or []:
            if str(row.get("ticker") or "") not in tickers:
                continue
            tier = kalshi_kickoff_tier(row, now)
            if tier == "UNKNOWN":
                unknown_kickoff += 1
                continue
            if tier == "STARTED":
                started += 1
                continue
            (hot if tier == "HOT" else warm).append(row)
        def kickoff_key(row: dict) -> tuple[str, str]:
            event = row.get("_discovery_event") or {}
            kickoff = _parse_utc(event.get("scheduled_start") or row.get("scheduled_start"))
            return (kickoff.isoformat() if kickoff else "", row["ticker"])
        hot.sort(key=kickoff_key)
        warm.sort(key=kickoff_key)
        buys = set(snapshot.get("buy_tickers") or [])
        priority = [row for row in hot if row["ticker"] in buys]
        other = [row for row in hot if row["ticker"] not in buys]
        buy_slots = min(
            len(priority),
            KALSHI_REFRESH_MAX_MARKETS - int(bool(other)),
        )
        buy_cursor = int(snapshot.get("buy_cursor") or 0)
        other_cursor = int(snapshot.get("other_cursor") or 0)

        def choose(items: list[dict], cursor: int, limit: int) -> list[dict]:
            """Start with earlier kickoffs, then rotate across the whole tier."""
            return [
                items[(cursor + index) % len(items)]
                for index in range(min(limit, len(items)))
            ] if items else []

        selected_buys = choose(priority, buy_cursor, buy_slots)
        selected_other = choose(other, other_cursor, KALSHI_REFRESH_MAX_MARKETS - len(selected_buys))
        selected = selected_buys + selected_other
        hot_selected = len(selected)
        warm_slots = KALSHI_REFRESH_MAX_MARKETS - hot_selected
        warm_cursor = int(snapshot.get("warm_cursor") or 0)
        selected_warm = [warm[(warm_cursor + index) % len(warm)] for index in range(min(warm_slots, len(warm)))] if warm else []
        selected.extend(selected_warm)
        selected_tickers = {row["ticker"] for row in selected}
        unrefreshed = [row["ticker"] for row in hot + warm if row["ticker"] not in selected_tickers]
        observed = snapshot.get("last_observed_at_by_ticker") or {}
        hot_ages = [(now - timestamp).total_seconds() for row in hot if (timestamp := _parse_utc(observed.get(row["ticker"]))) is not None]
        unknown_hot_ages = len(hot) - len(hot_ages)
        hot_omitted = len(hot) - hot_selected
        warm_omitted = len(warm) - len(selected_warm)
        return selected, {
            "known_market_count": len(hot) + len(warm),
            "selected_market_count": len(selected),
            "unrefreshed_market_count": len(unrefreshed),
            "unrefreshed_tickers": unrefreshed,
            "started_market_count": started,
            "unknown_kickoff_market_count": unknown_kickoff,
            "hot_known_count": len(hot),
            "hot_selected_count": hot_selected,
            "hot_omitted_count": hot_omitted,
            "warm_known_count": len(warm),
            "warm_selected_count": len(selected_warm),
            "warm_omitted_count": warm_omitted,
            "hot_coverage_state": "PARTIAL" if hot_omitted or unknown_kickoff or not snapshot.get("discovery_complete") else "PENDING_BOOKS",
            "warm_coverage_state": "ROTATING" if warm else "NOT_APPLICABLE",
            "oldest_hot_refresh_age_seconds": max(hot_ages) if hot_ages else None,
            "hot_unknown_refresh_age_count": unknown_hot_ages,
            "priority_buy_count": len(priority),
            "priority_buy_selected": len(selected_buys),
            "continuous_coverage": not unrefreshed and not unknown_kickoff and bool(snapshot.get("discovery_complete")),
            "discovery_complete": bool(snapshot.get("discovery_complete")),
            "nominal_rotation_cycles": max(ceil(len(priority) / buy_slots) if buy_slots else 0, ceil(len(other) / len(selected_other)) if selected_other else 0, ceil(len(warm) / len(selected_warm)) if selected_warm else 0),
            "next_buy_cursor": buy_cursor + len(selected_buys),
            "next_other_cursor": other_cursor + len(selected_other),
            "next_warm_cursor": warm_cursor + len(selected_warm),
        }

    def record_kalshi_discovery(
        self,
        rows: list[dict],
        *,
        actionable_tickers: set[str],
        refreshed_at: datetime,
        coverage_complete: bool = True,
        observed_at_by_ticker: Mapping[str, str] | None = None,
    ) -> None:
        refresh_tickers = list(dict.fromkeys(str(row["ticker"]) for row in rows))
        _atomic_write_json(
            self.kalshi_path,
            {
                "schema_version": 2,
                "discovered_at": refreshed_at.astimezone(UTC).isoformat(),
                "last_refreshed_at": refreshed_at.astimezone(UTC).isoformat(),
                "markets": rows,
                "refresh_tickers": refresh_tickers,
                "discovery_complete": coverage_complete,
                "buy_tickers": sorted(actionable_tickers & set(refresh_tickers)),
                "buy_cursor": 0,
                "other_cursor": 0,
                "warm_cursor": 0,
                "last_observed_at_by_ticker": {
                    ticker: value for ticker, value in (observed_at_by_ticker or {}).items()
                    if ticker in refresh_tickers
                },
            },
        )

    def record_kalshi_refresh(
        self,
        refreshed_at: datetime,
        *,
        observed_tickers: set[str],
        actionable_tickers: set[str],
        next_buy_cursor: int,
        next_other_cursor: int,
        next_warm_cursor: int = 0,
        observed_at_by_ticker: Mapping[str, str] | None = None,
    ) -> None:
        snapshot = self.kalshi_snapshot()
        if snapshot["state"] != "READY":
            return
        current = set(snapshot["refresh_tickers"])
        buys = (
            set(snapshot["buy_tickers"]) - observed_tickers
        ) | actionable_tickers
        _atomic_write_json(
            self.kalshi_path,
            {
                "schema_version": 2,
                "discovered_at": snapshot["discovered_at"],
                "last_refreshed_at": refreshed_at.astimezone(UTC).isoformat(),
                "markets": snapshot["markets"],
                "refresh_tickers": snapshot["refresh_tickers"],
                "discovery_complete": snapshot["discovery_complete"],
                "buy_tickers": sorted(buys & current),
                "buy_cursor": next_buy_cursor,
                "other_cursor": next_other_cursor,
                "warm_cursor": next_warm_cursor,
                "last_observed_at_by_ticker": {
                    **snapshot["last_observed_at_by_ticker"],
                    **{
                        ticker: value for ticker, value in (observed_at_by_ticker or {}).items()
                        if ticker in current
                    },
                },
            },
        )

    def pmus_status(self) -> dict[str, Any]:
        """Return a bounded fail-closed status even when state is malformed."""
        now = self._now()
        try:
            payload = self._read(self.pmus_path)
            if payload is None or payload.get("schema_version") != 1:
                if payload is None:
                    return {"state": "READY", "attempt_allowed": True, "retry_at": None}
                raise ValueError("invalid PMUS cooldown state")
            retry_at = _parse_utc(payload.get("retry_at"))
            if retry_at is None:
                return {"state": "READY", "attempt_allowed": True, "retry_at": None}
            active = now < retry_at
            return {
                "state": "COOLDOWN" if active else "READY",
                "attempt_allowed": not active,
                "failed_at": payload.get("failed_at"),
                "retry_at": retry_at.isoformat(),
                "failure_classification": payload.get("failure_classification"),
                "reason": payload.get("reason"),
                "seconds_remaining": max(0.0, (retry_at - now).total_seconds()),
            }
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            try:
                modified = datetime.fromtimestamp(self.pmus_path.stat().st_mtime, UTC)
            except OSError:
                modified = now
            retry_at = modified + timedelta(seconds=PMUS_STARTUP_COOLDOWN_SECONDS)
            active = now < retry_at
            return {
                "state": "CORRUPT_COOLDOWN" if active else "READY",
                "attempt_allowed": not active,
                "retry_at": retry_at.isoformat(),
                "failure_classification": "CORRUPT_STATE",
                "reason": "malformed PMUS cooldown state",
                "seconds_remaining": max(0.0, (retry_at - now).total_seconds()),
            }

    def record_pmus_failure(self, *, classification: str, reason: str) -> dict[str, Any]:
        now = self._now()
        retry_at = now + timedelta(seconds=PMUS_STARTUP_COOLDOWN_SECONDS)
        _atomic_write_json(
            self.pmus_path,
            {
                "schema_version": 1,
                "failed_at": now.isoformat(),
                "retry_at": retry_at.isoformat(),
                "failure_classification": classification,
                "reason": reason[:240],
            },
        )
        return self.pmus_status()

    def record_pmus_success(self) -> None:
        _atomic_write_json(
            self.pmus_path,
            {"schema_version": 1, "retry_at": None, "reason": None},
        )
