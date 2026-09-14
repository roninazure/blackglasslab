#!/usr/bin/env python3
"""Run one bounded prospective outcome reconciliation pass."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parallax.prospective_reconciliation import ProspectiveReconciler
from parallax.track_record import TrackRecord


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/parallax-commercial/prospective.sqlite"))
    parser.add_argument("--limit", type=int, default=500)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 5_000:
        parser.error("--limit must be between 1 and 5000")
    report = ProspectiveReconciler(TrackRecord(args.db)).reconcile(limit=args.limit)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
