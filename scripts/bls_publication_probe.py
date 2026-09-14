#!/usr/bin/env python3
"""Measure the September 3 BLS Productivity publication arrival."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date, datetime, time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from flash_employment.productivity_probe import EASTERN, ProbeConfig, run_probe


def hhmm(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("time must use HH:MM or HH:MM:SS") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", choices=("productivity",), default="productivity")
    parser.add_argument("--event-date", type=date.fromisoformat, default=date(2026, 9, 3))
    parser.add_argument("--release-time", type=hhmm, default=time(8, 30))
    parser.add_argument("--duration-minutes", type=float, default=5.0)
    parser.add_argument(
        "--db",
        type=Path,
        default=ROOT / "trials" / "bls_publication_probe_20260903.sqlite",
    )
    args = parser.parse_args()
    if args.duration_minutes <= 0:
        parser.error("duration-minutes must be positive")
    config = ProbeConfig(
        event_date=args.event_date,
        release_at=datetime.combine(args.event_date, args.release_time, tzinfo=EASTERN),
        duration_minutes=args.duration_minutes,
        db_path=args.db,
        contact=os.environ.get("FLASH_CONTACT", ""),
    )
    try:
        run_id, result = asyncio.run(run_probe(config))
    except KeyboardInterrupt:
        print("BLS publication probe stopped", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI fail-closed boundary
        print(f"BLS publication probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"run_id": run_id, "db": str(args.db), **result}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
