#!/usr/bin/env python3
"""Run the bounded, paper-only BLS Employment Situation FLASH trial."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flash_employment.trial import (
    DEFAULT_EVENT_SLUG,
    EASTERN,
    TrialConfig,
    run_config,
)


def hhmm(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must use HH:MM or HH:MM:SS") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rehearsal",
        action="store_true",
        help="use latest historical BLS release and clearly mark counterfactual results",
    )
    parser.add_argument(
        "--event-date", type=date.fromisoformat, default=date(2026, 9, 4)
    )
    parser.add_argument("--arm-at", type=hhmm, default=time(8, 25))
    parser.add_argument("--release-time", type=hhmm, default=time(8, 30))
    parser.add_argument("--duration-minutes", type=float, default=15.0)
    parser.add_argument(
        "--db", type=Path, default=ROOT / "trials" / "flash_employment_20260904.sqlite"
    )
    parser.add_argument("--event-slug", default=DEFAULT_EVENT_SLUG)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument(
        "--rehearsal-seconds", type=float, default=8.0, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--preflight-only",
        action="store_true",
        help="validate all Friday arm gates without accepting a release",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if (
        args.duration_minutes <= 0
        or args.slippage_bps < 0
        or args.rehearsal_seconds <= 0
    ):
        parser.error(
            "duration and rehearsal seconds must be positive; slippage cannot be negative"
        )
    arm_at = datetime.combine(args.event_date, args.arm_at, tzinfo=EASTERN)
    release_at = datetime.combine(args.event_date, args.release_time, tzinfo=EASTERN)
    if arm_at >= release_at:
        parser.error("arm-at must precede release-time")
    config = TrialConfig(
        rehearsal=args.rehearsal,
        db_path=args.db,
        event_date=args.event_date,
        arm_at=arm_at,
        release_at=release_at,
        duration_minutes=args.duration_minutes,
        event_slug=args.event_slug,
        slippage_bps=args.slippage_bps,
        rehearsal_seconds=args.rehearsal_seconds,
        preflight_only=args.preflight_only,
    )
    try:
        run_id, result = asyncio.run(run_config(config))
    except KeyboardInterrupt:
        print("FLASH trial stopped", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI boundary must fail closed with one-line evidence
        print(
            f"FLASH trial failed closed: {type(exc).__name__}: {exc}", file=sys.stderr
        )
        return 1
    output = {
        "run_id": run_id,
        "mode": (
            "PREFLIGHT_ONLY"
            if args.preflight_only
            else "REHEARSAL" if args.rehearsal else "LIVE_OBSERVATION"
        ),
        "db": str(args.db),
        **result,
    }
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
