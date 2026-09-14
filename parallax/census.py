"""Bounded public-data census. No PlayService (which auto-publishes BUYs)."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from .engine import qualify
from .models import Side, utcnow
from .sources import collect_markets


def run_census(limit=16):
    markets, collection = collect_markets(limit)
    now = utcnow()
    rows = []
    for market in markets:
        for side in Side:
            play = qualify(market, side, now=now)
            small = play.retail_examples[1]
            rows.append(
                {
                    "venue": market.venue,
                    "market_id": market.venue_market_id,
                    "title": market.title,
                    "side": side,
                    "executable_price": play.current_price,
                    "fair_value": play.parallax_fair_value,
                    "fair_value_provenance": market.original_metadata[
                        "fair_value_provenance"
                    ],
                    "gross_edge_points": play.edge_points,
                    "fee_estimate_25": play.fees_estimate,
                    "fee_estimate_kind": market.mechanics.fee_status,
                    "fee_provenance": market.original_metadata["fee_provenance"],
                    "fee_mechanics": asdict(market.mechanics),
                    "slippage_estimate_25": play.slippage_estimate,
                    "depth_treatment": "Limit price at top ask only; zero price slippage conditional on fill; no sweep or extrapolation",
                    "net_edge_points": (
                        100 * play.expected_value / small.contracts_or_shares
                        if play.expected_value is not None and small.contracts_or_shares
                        else None
                    ),
                    "available_depth_contracts": play.executable_size,
                    "resolution_time": play.resolution_time,
                    "verdict": play.suggested_action,
                    "qualification_reason": play.reason_summary,
                    "blocking_gates": play.verdict.failed_gates,
                    "invalidation_reasons": play.invalidation_conditions,
                    "book_timestamp": market.book_timestamp,
                    "data_timestamp": market.data_timestamp,
                    "freshness": play.data_freshness,
                    "qualified_at": now.isoformat(),
                    "retail_examples": [asdict(x) for x in play.retail_examples],
                }
            )
    return {
        "as_of": now.isoformat(),
        "collection": collection,
        "markets_by_venue": dict(Counter(m.venue for m in markets)),
        "verdicts_by_venue": {
            venue: dict(Counter(r["verdict"] for r in rows if r["venue"] == venue))
            for venue in sorted({m.venue for m in markets})
        },
        "blocking_gates": dict(
            Counter(gate for r in rows for gate in r["blocking_gates"])
        ),
        "fair_value_status": dict(
            Counter(
                m.original_metadata["fair_value_provenance"]["status"] for m in markets
            )
        ),
        "fee_status": dict(Counter(m.mechanics.fee_status for m in markets)),
        "verdicts": dict(Counter(x["verdict"] for x in rows)),
        "live_orders": 0,
        "capital_deployed": 0,
        "execution_enabled": False,
        "published": False,
        "candidates": rows,
        "market_snapshots": [asdict(m) for m in markets],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=16)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive creation avoids replacing a previous observation.
    with args.output.open("x") as output:
        report = run_census(args.limit)
        json.dump(report, output, indent=2, allow_nan=False)
    print(
        json.dumps(
            {
                k: v
                for k, v in report.items()
                if k not in {"candidates", "market_snapshots"}
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
