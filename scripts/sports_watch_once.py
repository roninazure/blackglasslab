#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


REPORT_PATH = Path("reports/sports_scan_latest.json")
QUOTA_PATH = Path("reports/sports_quota_state.json")
WATCH_STATE_PATH = Path("reports/sports_watch_state.json")

QUOTA_RESERVE = 5


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).astimezone(timezone.utc)


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


def save_watch_state(payload: dict) -> None:
    WATCH_STATE_PATH.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    WATCH_STATE_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n"
    )


def quota_blocked(
    quota: dict | None,
    *,
    now: datetime,
) -> bool:
    if not quota:
        return False

    remaining = quota.get("remaining")
    reset_epoch = quota.get("reset_epoch")

    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        return False

    if remaining > QUOTA_RESERVE:
        return False

    if reset_epoch is None:
        return True

    try:
        reset = datetime.fromtimestamp(
            int(reset_epoch),
            tz=timezone.utc,
        )
    except (TypeError, ValueError, OSError):
        return True

    return now < reset


def next_future_start(
    report: dict | None,
    *,
    now: datetime,
) -> datetime | None:
    if not report:
        return None

    starts = []

    for row in report.get("evaluations") or []:
        if not isinstance(row, dict):
            continue

        value = row.get("start_time")
        if not value:
            continue

        try:
            start = parse_utc(str(value))
        except ValueError:
            continue

        if start > now:
            starts.append(start)

    return min(starts) if starts else None


def cadence_seconds(
    next_start: datetime | None,
    *,
    now: datetime,
) -> int | None:
    if next_start is None:
        return None

    seconds = (next_start - now).total_seconds()

    if seconds > 6 * 3600:
        return None

    if seconds > 2 * 3600:
        return 3600

    if seconds > 30 * 60:
        return 15 * 60

    if seconds > 0:
        return 5 * 60

    return None


def scan_due(
    *,
    now: datetime,
    cadence: int | None,
    watch_state: dict | None,
) -> bool:
    if cadence is None:
        return False

    if not watch_state:
        return True

    last = watch_state.get("last_scan_at_utc")

    if not last:
        return True

    try:
        last_dt = parse_utc(str(last))
    except ValueError:
        return True

    return (now - last_dt).total_seconds() >= cadence


def main() -> int:
    now = datetime.now(timezone.utc)

    quota = load_json(QUOTA_PATH)

    if quota_blocked(quota, now=now):
        print("BLOCK_QUOTA_RESERVE")
        return 0

    report = load_json(REPORT_PATH)
    next_start = next_future_start(
        report,
        now=now,
    )

    cadence = cadence_seconds(
        next_start,
        now=now,
    )

    watch_state = load_json(WATCH_STATE_PATH)

    if not scan_due(
        now=now,
        cadence=cadence,
        watch_state=watch_state,
    ):
        print("SKIP_NOT_DUE")
        return 0

    print("SCAN_DUE")

    scan = subprocess.run(
        [
            sys.executable,
            "scripts/sports_scan.py",
        ],
        check=False,
    )

    if scan.returncode != 0:
        return scan.returncode

    save_watch_state({
        "last_scan_at_utc": now.isoformat(),
    })

    alert = subprocess.run(
        [
            sys.executable,
            "scripts/sports_alert_gate.py",
        ],
        check=False,
    )

    return alert.returncode


if __name__ == "__main__":
    raise SystemExit(main())
