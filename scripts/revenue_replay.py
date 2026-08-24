#!/usr/bin/env python3
"""Read-only Revenue admission replay; it never creates or modifies data."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# ``scripts/revenue_poc.py`` would otherwise shadow the revenue_poc package
# when this file is invoked directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from revenue_poc.replay import baseline_policy, format_report, open_readonly, replay


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--start", required=True, help="inclusive ISO-8601 timestamp")
    parser.add_argument("--end", required=True, help="exclusive ISO-8601 timestamp")
    parser.add_argument("--candidate-min-edge", type=float)
    parser.add_argument("--candidate-position-size", type=float)
    parser.add_argument("--candidate-max-open-positions", type=int)
    parser.add_argument("--candidate-max-deployed", type=float)
    parser.add_argument("--candidate-max-category-positions", type=int)
    args = parser.parse_args()
    with open_readonly(args.db) as conn:
        baseline = baseline_policy(conn)
        candidate = baseline.with_overrides(
            min_executable_edge=args.candidate_min_edge, position_size_usd=args.candidate_position_size,
            max_open_positions=args.candidate_max_open_positions, max_capital_deployed_usd=args.candidate_max_deployed,
            max_category_positions=args.candidate_max_category_positions,
        )
        print(format_report(replay(conn, start=args.start, end=args.end, policy=baseline), replay(conn, start=args.start, end=args.end, policy=candidate)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
