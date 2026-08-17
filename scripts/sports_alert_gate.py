#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
from pathlib import Path


DEFAULT_REPORT = "reports/sports_scan_latest.json"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Parallax Sports PAPER_ONLY alert gate"
    )
    parser.add_argument(
        "--report",
        default=DEFAULT_REPORT,
    )
    args = parser.parse_args()

    path = Path(args.report)

    if not path.exists():
        raise SystemExit(
            f"ERROR: sports scan report not found: {path}"
        )

    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(
            f"ERROR: invalid sports scan report: {exc}"
        )

    if payload.get("mode") != "PAPER_ONLY":
        raise SystemExit(
            "ERROR: alert gate accepts PAPER_ONLY reports only"
        )

    evaluations = payload.get("evaluations")

    if not isinstance(evaluations, list):
        raise SystemExit(
            "ERROR: evaluations missing or invalid"
        )

    candidates = [
        row
        for row in evaluations
        if isinstance(row, dict)
        and row.get("decision") == "PAPER_CANDIDATE"
    ]

    declared = payload.get("paper_candidate_count")

    if declared != len(candidates):
        raise SystemExit(
            "ERROR: candidate-count integrity check failed"
        )

    # Silence is intentional when no qualified opportunity exists.
    if not candidates:
        return 0

    candidates.sort(
        key=lambda row: float(
            row.get("executable_edge", 0.0)
        ),
        reverse=True,
    )

    for row in candidates:
        print("PARALLAX SPORTS — PAPER OPPORTUNITY")
        print(
            f"{str(row.get('sport', '')).upper()} | "
            f"{row.get('matchup')}"
        )
        print(f"Team: {row.get('selected_team')}")
        print(
            "Fair probability: "
            f"{float(row['fair_probability']):.2%}"
        )
        print(
            "Executable entry: "
            f"{float(row['entry_price']):.2%}"
        )
        print(
            "Net executable edge: "
            f"{float(row['executable_edge']):+.2%}"
        )
        print(
            "Expected value: "
            f"${float(row['expected_value_usd']):+.3f}"
        )
        print(
            "Top-book depth: "
            f"${float(row['depth_usd']):,.2f}"
        )
        print(
            "Stake: "
            f"${float(row['stake_usd']):.2f}"
        )
        print("MODE: PAPER_ONLY")
        print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
