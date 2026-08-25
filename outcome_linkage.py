from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parent
SCHEMA = ROOT / "migrations" / "010_market_resolutions_v1.sql"


class ResolutionConflictError(RuntimeError):
    """A canonical market already has a different trusted outcome."""


@dataclass(frozen=True)
class MarketResolution:
    id: int
    resolution_key: str
    venue: str
    market_id: str
    resolved_outcome: str
    resolved_at_utc: str
    resolution_source: str
    source_reference: str | None
    recorded_at_utc: str
    provenance_metadata: str
    inserted: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _required_text(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    return text


def _metadata(value: Mapping[str, Any] | None) -> str:
    return json.dumps(dict(value or {}), sort_keys=True, separators=(",", ":"))


def ensure_resolution_schema(conn: sqlite3.Connection) -> None:
    # executescript implicitly commits an active transaction. Execute complete
    # statements individually so schema setup respects caller ownership.
    statement = ""
    for line in SCHEMA.read_text(encoding="utf-8").splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        conn.execute(statement)


def record_market_resolution(
    conn: sqlite3.Connection,
    *,
    venue: str,
    market_id: str,
    outcome: str,
    resolved_at_utc: str,
    resolution_source: str,
    source_reference: str | None = None,
    recorded_at_utc: str | None = None,
    provenance_metadata: Mapping[str, Any] | None = None,
    commit: bool = True,
) -> MarketResolution:
    """Insert one canonical resolution, or return the identical existing one.

    A different outcome for the same venue/market is a fail-closed conflict;
    the original canonical row is never updated or deleted.
    """
    ensure_resolution_schema(conn)
    venue_text = _required_text(venue, "venue")
    market_text = _required_text(market_id, "market_id")
    outcome_text = _required_text(outcome, "outcome").upper()
    if outcome_text not in {"YES", "NO"}:
        raise ValueError("outcome must be YES or NO")
    resolved_text = _required_text(resolved_at_utc, "resolved_at_utc")
    source_text = _required_text(resolution_source, "resolution_source")
    recorded_text = recorded_at_utc or _utc_now()
    key = f"{venue_text}:{market_text}"
    metadata_text = _metadata(provenance_metadata)
    try:
        cursor = conn.execute(
            """
            INSERT INTO market_resolutions
            (resolution_key,venue,market_id,resolved_outcome,resolved_at_utc,
             resolution_source,source_reference,recorded_at_utc,provenance_metadata)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (key, venue_text, market_text, outcome_text, resolved_text,
             source_text, source_reference, recorded_text, metadata_text),
        )
        row_id = int(cursor.lastrowid)
        inserted = True
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM market_resolutions WHERE venue=? AND market_id=?",
            (venue_text, market_text),
        ).fetchone()
        if existing is None:
            raise
        existing_outcome = (
            str(existing["resolved_outcome"])
            if isinstance(existing, sqlite3.Row)
            else str(existing[4])
        )
        if existing_outcome != outcome_text:
            raise ResolutionConflictError(
                f"conflicting resolution for {venue_text}:{market_text}: "
                f"existing={existing_outcome} "
                f"incoming={outcome_text}"
            )
        row_id = int(existing["id"] if isinstance(existing, sqlite3.Row) else existing[0])
        inserted = False
    if commit:
        conn.commit()
    row = conn.execute(
        "SELECT * FROM market_resolutions WHERE id=?", (row_id,)
    ).fetchone()
    if row is None:
        raise RuntimeError("canonical resolution could not be read after persistence")
    if not isinstance(row, sqlite3.Row):
        keys = [column[1] for column in conn.execute("PRAGMA table_info(market_resolutions)")]
        row = dict(zip(keys, row))
    return MarketResolution(
        id=int(row["id"]), resolution_key=str(row["resolution_key"]),
        venue=str(row["venue"]), market_id=str(row["market_id"]),
        resolved_outcome=str(row["resolved_outcome"]),
        resolved_at_utc=str(row["resolved_at_utc"]),
        resolution_source=str(row["resolution_source"]),
        source_reference=row["source_reference"],
        recorded_at_utc=str(row["recorded_at_utc"]),
        provenance_metadata=str(row["provenance_metadata"]), inserted=inserted,
    )


def linked_forecasts(
    conn: sqlite3.Connection, *, venue: str, market_id: str
) -> list[sqlite3.Row]:
    """Return all forecasts for a market with its canonical resolution, if any."""
    ensure_resolution_schema(conn)
    conn.row_factory = sqlite3.Row
    return conn.execute(
        """
        SELECT f.*, r.id AS resolution_id,
               r.resolved_outcome AS canonical_resolved_outcome,
               r.resolved_at_utc AS canonical_resolved_at_utc,
               r.resolution_source, r.source_reference,
               r.recorded_at_utc AS resolution_recorded_at_utc,
               r.provenance_metadata AS resolution_provenance_metadata
        FROM shadow_forecasts f
        LEFT JOIN market_resolutions r
          ON r.venue=f.venue AND r.market_id=f.market_id
        WHERE f.venue=? AND f.market_id=?
        ORDER BY f.timestamp_utc, f.id
        """,
        (venue, market_id),
    ).fetchall()
