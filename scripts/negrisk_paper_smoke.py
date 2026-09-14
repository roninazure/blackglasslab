#!/usr/bin/env python3
"""Bounded READ/PAPER-only live-data economic observations."""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from negrisk_economics.live import ReadOnlyPublicClient, discover_events, fetch_basket_books  # noqa: E402
from negrisk_economics.model import BasketEvaluation, ValidationConfig, evaluate_basket  # noqa: E402
from negrisk_economics.paper import initialize_paper_db, record_prediction  # noqa: E402


DISCOVERY_REFRESH_SECONDS = 60.0


class CandidateRoundRobin:
    def __init__(self) -> None:
        self._events: list[dict] = []
        self._cursor = 0
        self.generation = 0

    def refresh(self, events: list[dict]) -> None:
        next_event_id = None
        if self._events:
            next_event = self._events[self._cursor]
            next_event_id = str(next_event.get("id") or next_event.get("slug"))
        self._events = list(events)
        refreshed_ids = self.event_ids
        if next_event_id in refreshed_ids:
            self._cursor = refreshed_ids.index(next_event_id)
        elif self._events:
            self._cursor %= len(self._events)
        else:
            self._cursor = 0
        self.generation += 1

    def __len__(self) -> int:
        return len(self._events)

    @property
    def event_ids(self) -> list[str]:
        return [str(event.get("id") or event.get("slug")) for event in self._events]

    def next(self) -> dict:
        if not self._events:
            raise LookupError("no NegRisk candidates available")
        event = self._events[self._cursor]
        self._cursor = (self._cursor + 1) % len(self._events)
        return event


def bounded_events(client: ReadOnlyPublicClient, *, limit: int) -> list[dict]:
    return [event for event in discover_events(client, limit=limit) if len(event.get("markets", [])) <= 8]


def evaluate_next_candidate(
    client: ReadOnlyPublicClient,
    candidates: CandidateRoundRobin,
    *,
    fee_info: dict,
    config: ValidationConfig,
    latency_seconds: float,
) -> tuple[dict | None, BasketEvaluation | None, dict, list[dict[str, str]]]:
    errors: list[dict[str, str]] = []
    for _ in range(len(candidates)):
        candidate = candidates.next()
        event_id = str(candidate.get("id") or candidate.get("slug"))
        try:
            initial, fee_info = fetch_basket_books(client, candidate, fee_info=fee_info)
            time.sleep(latency_seconds)
            execution, fee_info = fetch_basket_books(client, candidate, fee_info=fee_info)
            evaluation = evaluate_basket(
                event_id=event_id,
                initial_books=initial,
                execution_books=execution,
                basket_complete=True,
                config=config,
            )
            return candidate, evaluation, fee_info, errors
        except Exception as exc:
            errors.append({"event_id": event_id, "error": f"{type(exc).__name__}: {exc}"})
    return None, None, fee_info, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Bounded negrisk PAPER_ONLY economic trial")
    parser.add_argument("--paper-db", required=True, help="new SQLite path; existing files are refused")
    parser.add_argument("--latency-seconds", type=float, default=1.0)
    parser.add_argument("--target-shares", type=float, default=5.0)
    parser.add_argument("--event-limit", type=int, default=100)
    parser.add_argument("--duration-minutes", type=float, default=0.0, help="zero runs one observation; maximum 240")
    parser.add_argument("--interval-seconds", type=float, default=30.0, help="start-to-start interval for bounded trials")
    parser.add_argument("--detach", action="store_true", help="start the bounded trial in a detached process")
    parser.add_argument("--log-path", help="fresh log path required with --detach")
    args = parser.parse_args()
    if not 0 <= args.latency_seconds <= 5:
        raise SystemExit("latency-seconds must be between 0 and 5")
    if not 0 <= args.duration_minutes <= 240:
        raise SystemExit("duration-minutes must be between 0 and 240")
    if args.duration_minutes and args.interval_seconds < 10:
        raise SystemExit("interval-seconds must be at least 10 for a bounded trial")
    path = Path(args.paper_db).resolve()
    if path.exists():
        raise SystemExit(f"refusing existing paper DB: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if args.detach:
        if not args.duration_minutes or not args.log_path:
            raise SystemExit("--detach requires positive --duration-minutes and --log-path")
        log_path = Path(args.log_path).resolve()
        if log_path.exists():
            raise SystemExit(f"refusing existing trial log: {log_path}")
        log_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--paper-db",
            str(path),
            "--latency-seconds",
            str(args.latency_seconds),
            "--target-shares",
            str(args.target_shares),
            "--event-limit",
            str(args.event_limit),
            "--duration-minutes",
            str(args.duration_minutes),
            "--interval-seconds",
            str(args.interval_seconds),
        ]
        with log_path.open("x", encoding="utf-8") as stream:
            process = subprocess.Popen(
                command,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=stream,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        print(json.dumps({"pid": process.pid, "command": command, "paper_db": str(path), "log_path": str(log_path)}, sort_keys=True))
        return 0

    started = time.monotonic()
    duration_seconds = args.duration_minutes * 60.0
    trial_deadline = started + duration_seconds if duration_seconds else started + 180.0
    client = ReadOnlyPublicClient(timeout_seconds=5.0, deadline_monotonic=trial_deadline + 5.0)
    conn = sqlite3.connect(path)
    initialize_paper_db(conn)
    candidates = CandidateRoundRobin()
    candidates.refresh(bounded_events(client, limit=args.event_limit))
    if not len(candidates):
        raise SystemExit("no standard complete event with at most eight legs in bounded sample")

    config = ValidationConfig(
        target_shares=args.target_shares,
        min_shares=min(args.target_shares, 1.0),
        latency_seconds=args.latency_seconds,
    )
    attempt = 0
    recorded = 0
    fee_info: dict = {}
    next_discovery_refresh = time.monotonic() + DISCOVERY_REFRESH_SECONDS
    try:
        while True:
            cycle_started = time.monotonic()
            attempt += 1
            if cycle_started >= next_discovery_refresh:
                try:
                    candidates.refresh(bounded_events(client, limit=args.event_limit))
                    print(
                        json.dumps(
                            {
                                "attempt": attempt,
                                "status": "DISCOVERY_REFRESH",
                                "candidate_count": len(candidates),
                                "candidate_event_ids": candidates.event_ids,
                                "discovery_generation": candidates.generation,
                            },
                            sort_keys=True,
                        ),
                        flush=True,
                    )
                except Exception as exc:
                    print(json.dumps({"attempt": attempt, "status": "DISCOVERY_ERROR", "error": f"{type(exc).__name__}: {exc}"}, sort_keys=True), flush=True)
                finally:
                    next_discovery_refresh = time.monotonic() + DISCOVERY_REFRESH_SECONDS

            selected, evaluation, fee_info, errors = evaluate_next_candidate(
                client,
                candidates,
                fee_info=fee_info,
                config=config,
                latency_seconds=args.latency_seconds,
            )
            for error in errors:
                print(json.dumps({"attempt": attempt, "status": "READ_ERROR", **error}, sort_keys=True), flush=True)
            if selected is not None and evaluation is not None:
                prediction_id = record_prediction(conn, evaluation)
                recorded += 1
                print(
                    json.dumps(
                        {
                            "attempt": attempt,
                            "recorded": recorded,
                            "paper_db": str(path),
                            "prediction_id": prediction_id,
                            "event_id": evaluation.event_id,
                            "market_count": len(selected.get("markets", [])),
                            "candidate_count": len(candidates),
                            "discovery_generation": candidates.generation,
                            "wall_seconds": round(time.monotonic() - started, 3),
                            "status": evaluation.status,
                            "economic_status": evaluation.economic_status,
                            "signal_gross_edge_per_share": evaluation.signal_gross_edge_per_share,
                            "gross_edge_usd": evaluation.gross_edge_usd,
                            "slippage_usd": evaluation.slippage_usd,
                            "fee_usd": evaluation.fee_usd,
                            "net_executable_edge_usd": evaluation.net_executable_edge_usd,
                            "fill_probability_status": evaluation.fill_probability_status,
                            "unresolved_unknowns": evaluation.unresolved_unknowns,
                            "transport": "public HTTPS GET only",
                            "execution_mode": evaluation.execution_mode,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
            if not duration_seconds:
                break
            remaining = trial_deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_seconds = min(max(0.0, args.interval_seconds - (time.monotonic() - cycle_started)), remaining)
            time.sleep(sleep_seconds)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
