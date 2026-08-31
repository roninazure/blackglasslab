#!/usr/bin/env python3

import os
import sqlite3

DB = os.path.expanduser(
    "~/Library/Application Support/SwarmEdge/state/runs.sqlite"
)

GAP_THRESHOLD = 0.10
EDGE_THRESHOLD = 0.05

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=10)
con.row_factory = sqlite3.Row

rows = con.execute("""
WITH ranked AS (
    SELECT
        e.*,
        d.decision,
        d.reason AS decision_reason,
        ROW_NUMBER() OVER (
            PARTITION BY e.market_id
            ORDER BY e.timestamp_utc DESC, e.id DESC
        ) AS rn
    FROM revenue_poc_evaluations e
    LEFT JOIN revenue_poc_decisions d
      ON d.evaluation_id = e.id
    WHERE e.timestamp_utc >= datetime('now','-24 hours')
      AND ABS(e.model_probability - e.market_probability) >= ?
      AND e.executable_edge >= ?
)
SELECT *
FROM ranked
WHERE rn = 1
ORDER BY executable_edge DESC
""", (GAP_THRESHOLD, EDGE_THRESHOLD)).fetchall()

print("=== SWARM EDGE ALERT PREVIEW — LAST 24H ===")
print(f"threshold: gap >= {GAP_THRESHOLD:.0%}, executable edge >= {EDGE_THRESHOLD:.0%}")
print(f"unique qualifying markets: {len(rows)}")
print()

for r in rows:
    gap = r["model_probability"] - r["market_probability"]

    if r["decision"] == "ADMIT":
        level = "OPPORTUNITY"
    elif r["production_decision"] == "candidate_pending_approval":
        level = "PENDING"
    else:
        level = "SIGNAL"

    print("=" * 78)
    print(f"SWARM EDGE — {level}")
    print(f"Question:       {r['question']}")
    print(f"Category:       {r['category']}")
    print(f"Venue:          {r['venue']}")
    print(f"Side:           {r['side']}")
    print(f"Model P:        {r['model_probability']:.1%}")
    print(f"Market P:       {r['market_probability']:.1%}")
    print(f"Model gap:      {gap:+.1%}")
    print(f"Executable edge:{r['executable_edge']:+.1%}")
    print(f"Expected EV:    ${r['expected_value_usd']:.2f}")
    print(f"Liquidity:      ${r['depth_usd']:,.0f}")
    print(f"Production:     {r['production_decision']}")
    print(f"Decision:       {r['decision'] or '-'}")
    print(f"Reason:         {r['decision_reason'] or r['production_rejection_reason'] or '-'}")
    print(f"Observed:       {r['timestamp_utc']}")

con.close()
