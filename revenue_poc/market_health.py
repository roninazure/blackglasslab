"""Append-only-safe stale market quarantine bookkeeping."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
def record_market_fetch(
    conn: sqlite3.Connection,
    *,
    venue: str,
    market_id: str,
    ok: bool,
    reason: str | None = None,
    threshold: int = 3,
    now: datetime | None = None,
) -> str:
    now_iso = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    row = conn.execute(
        "SELECT consecutive_failures FROM revenue_poc_market_health WHERE venue=? AND market_id=?",
        (venue, market_id),
    ).fetchone()
    failures = int(row[0]) if row else 0
    if ok:
        with conn:
            conn.execute(
                """INSERT INTO revenue_poc_market_health
                (venue,market_id,consecutive_failures,status,last_failure_reason,metadata)
                VALUES (?,?,0,'ACTIVE',NULL,'{}')
                ON CONFLICT(venue,market_id) DO UPDATE SET
                  consecutive_failures=0,status='ACTIVE',last_failure_reason=NULL""",
                (venue, market_id),
            )
        return "ACTIVE"
    failures += 1
    status = "QUARANTINED" if failures >= max(1, threshold) else "FAILING"
    with conn:
        conn.execute(
            """INSERT INTO revenue_poc_market_health
            (venue,market_id,consecutive_failures,status,last_failure_reason,
             first_failure_at_utc,last_failure_at_utc,quarantined_at_utc,metadata)
            VALUES (?,?,?,?,?,?,?,?,?)
            ON CONFLICT(venue,market_id) DO UPDATE SET
              consecutive_failures=excluded.consecutive_failures,
              status=excluded.status,last_failure_reason=excluded.last_failure_reason,
              last_failure_at_utc=excluded.last_failure_at_utc,
              quarantined_at_utc=CASE WHEN excluded.status='QUARANTINED'
                THEN COALESCE(revenue_poc_market_health.quarantined_at_utc,excluded.quarantined_at_utc)
                ELSE revenue_poc_market_health.quarantined_at_utc END""",
            (venue, market_id, failures, status, reason, now_iso, now_iso, now_iso if status == "QUARANTINED" else None, json.dumps({"threshold": threshold})),
        )
    return status


def is_quarantined(conn: sqlite3.Connection, *, venue: str, market_id: str) -> bool:
    row = conn.execute(
        "SELECT status FROM revenue_poc_market_health WHERE venue=? AND market_id=?",
        (venue, market_id),
    ).fetchone()
    return bool(row and row[0] == "QUARANTINED")
