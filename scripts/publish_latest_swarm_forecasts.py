#!/usr/bin/env python3
import json
import sqlite3
from datetime import datetime, timezone

DB_PATH = "memory/runs.sqlite"
VENUE = "swarm"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Grab latest arbiter run deterministically, then join runs for market_id/question
    row = cur.execute(
        """
        SELECT
          a.run_id,
          a.ts_utc,
          a.consensus_side,
          a.consensus_p_yes,
          a.disagreement,
          a.winner_agent,
          a.winner_fitness,
          a.notes,
          r.market_id,
          r.question
        FROM arbiter_runs a
        JOIN runs r ON r.run_id = a.run_id
        ORDER BY a.id DESC
        LIMIT 1;
        """
    ).fetchone()

    if not row:
        raise SystemExit("PUBLISH FAIL: no arbiter_runs found (run orchestrator first).")

    (
        run_id,
        arb_ts_utc,
        consensus_side,
        consensus_p_yes,
        disagreement,
        winner_agent,
        winner_fitness,
        arb_notes,
        market_id,
        question,
    ) = row

    ts_utc = utc_now_iso()

    notes = {
        "published_at_utc": ts_utc,
        "arbiter_ts_utc": arb_ts_utc,
        "consensus_side": consensus_side,
        "winner_agent": winner_agent,
        "winner_fitness": winner_fitness,
        "question": question,
        "arbiter_notes": arb_notes,
    }

    # IMPORTANT:
    # Your DB enforces UNIQUE(venue, market_id), so publish must UPSERT (update latest).
    cur.execute(
        """
        INSERT INTO model_forecasts (venue, market_id, ts_utc, p_yes_model, disagreement, run_id, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(venue, market_id) DO UPDATE SET
          ts_utc=excluded.ts_utc,
          p_yes_model=excluded.p_yes_model,
          disagreement=excluded.disagreement,
          run_id=excluded.run_id,
          notes=excluded.notes;
        """,
        (
            VENUE,
            market_id,
            ts_utc,
            float(consensus_p_yes),
            float(disagreement),
            run_id,
            json.dumps(notes, ensure_ascii=False),
        ),
    )

    conn.commit()
    conn.close()

    print(
        f"PUBLISH OK: venue={VENUE} market_id={market_id} "
        f"p_yes={float(consensus_p_yes):.6f} disagree={float(disagreement):.6f} run_id={run_id}"
    )


if __name__ == "__main__":
    main()
