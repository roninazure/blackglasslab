from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .fill import HypotheticalMakerQuote, MakerFillFollowup
from .model import MakerEvaluation


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS paper_maker_predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_key TEXT NOT NULL UNIQUE,
  recorded_at_utc TEXT NOT NULL,
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  execution_mode TEXT NOT NULL CHECK (execution_mode='PAPER_ONLY'),
  status TEXT NOT NULL,
  economic_status TEXT NOT NULL,
  spread_per_share REAL NOT NULL,
  conditional_size_shares REAL NOT NULL,
  paper_filled_shares REAL NOT NULL CHECK (paper_filled_shares=0),
  captured_spread_usd REAL,
  maker_rebate_usd REAL,
  adverse_selection_cost_usd REAL,
  applicable_maker_fees_usd REAL,
  conditional_maker_edge_usd REAL,
  fill_probability REAL,
  fill_probability_status TEXT NOT NULL,
  fill_adjusted_expected_edge_usd REAL,
  prediction_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_maker_followups (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER NOT NULL REFERENCES paper_maker_predictions(id),
  observed_at_utc TEXT NOT NULL,
  evidence_source TEXT NOT NULL,
  maker_fill_observed INTEGER,
  later_midpoint REAL,
  realized_conditional_edge_usd REAL,
  followup_window_seconds REAL,
  bid_fill_evidence_state TEXT,
  ask_fill_evidence_state TEXT,
  two_sided_completion_state TEXT,
  inventory_risk_state TEXT,
  hypothetical_inventory_shares REAL,
  conservative_fill_probability REAL,
  conservative_fill_probability_status TEXT,
  evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_maker_hypothetical_quotes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER NOT NULL REFERENCES paper_maker_predictions(id),
  market_id TEXT NOT NULL,
  token_id TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('BID','ASK')),
  quote_price REAL NOT NULL,
  quote_size_shares REAL NOT NULL,
  signaled_at_utc TEXT NOT NULL,
  eligible_from_utc TEXT NOT NULL,
  displayed_depth_ahead_shares REAL NOT NULL,
  displayed_depth_at_quote_shares REAL NOT NULL,
  evidence_json TEXT NOT NULL,
  UNIQUE(prediction_id,side)
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def initialize_paper_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def record_prediction(
    conn: sqlite3.Connection, evaluation: MakerEvaluation, *, recorded_at_utc: str | None = None
) -> int:
    if evaluation.execution_mode != "PAPER_ONLY" or evaluation.paper_filled_shares != 0:
        raise ValueError("maker validation records must remain unfilled PAPER_ONLY observations")
    payload = evaluation.as_dict()
    encoded = _json(payload)
    timestamp = recorded_at_utc or _now()
    key = hashlib.sha256(f"{timestamp}:{encoded}".encode()).hexdigest()
    with conn:
        cursor = conn.execute(
            """INSERT INTO paper_maker_predictions
            (record_key,recorded_at_utc,market_id,token_id,execution_mode,status,economic_status,
             spread_per_share,conditional_size_shares,paper_filled_shares,captured_spread_usd,
             maker_rebate_usd,adverse_selection_cost_usd,applicable_maker_fees_usd,
             conditional_maker_edge_usd,fill_probability,fill_probability_status,
             fill_adjusted_expected_edge_usd,prediction_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                timestamp,
                evaluation.market_id,
                evaluation.token_id,
                evaluation.execution_mode,
                evaluation.status,
                evaluation.economic_status,
                evaluation.spread_per_share,
                evaluation.conditional_size_shares,
                evaluation.paper_filled_shares,
                evaluation.captured_spread_usd,
                evaluation.maker_rebate_usd,
                evaluation.adverse_selection_cost_usd,
                evaluation.applicable_maker_fees_usd,
                evaluation.conditional_maker_edge_usd,
                evaluation.fill_probability,
                evaluation.fill_probability_status,
                evaluation.fill_adjusted_expected_edge_usd,
                encoded,
            ),
        )
    return int(cursor.lastrowid)


def record_followup(
    conn: sqlite3.Connection,
    prediction_id: int,
    *,
    observed_at_utc: str,
    evidence_source: str,
    maker_fill_observed: bool | None,
    later_midpoint: float | None,
    realized_conditional_edge_usd: float | None,
    evidence: dict[str, Any] | None = None,
    fill_followup: MakerFillFollowup | None = None,
    conservative_fill_probability: float | None = None,
    conservative_fill_probability_status: str | None = None,
) -> int:
    if conn.execute("SELECT 1 FROM paper_maker_predictions WHERE id=?", (prediction_id,)).fetchone() is None:
        raise ValueError("prediction does not exist")
    with conn:
        cursor = conn.execute(
            """INSERT INTO paper_maker_followups
            (prediction_id,observed_at_utc,evidence_source,maker_fill_observed,later_midpoint,
             realized_conditional_edge_usd,followup_window_seconds,bid_fill_evidence_state,
             ask_fill_evidence_state,two_sided_completion_state,inventory_risk_state,
             hypothetical_inventory_shares,conservative_fill_probability,
             conservative_fill_probability_status,evidence_json)
             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id,
                observed_at_utc,
                evidence_source,
                None if maker_fill_observed is None else int(maker_fill_observed),
                later_midpoint,
                realized_conditional_edge_usd,
                fill_followup.followup_window_seconds if fill_followup else None,
                fill_followup.bid.state if fill_followup else None,
                fill_followup.ask.state if fill_followup else None,
                fill_followup.two_sided_completion_state if fill_followup else None,
                fill_followup.inventory_risk_state if fill_followup else None,
                fill_followup.hypothetical_inventory_shares if fill_followup else None,
                conservative_fill_probability,
                conservative_fill_probability_status,
                _json(fill_followup.as_dict() if fill_followup else (evidence or {})),
            ),
        )
    return int(cursor.lastrowid)


def record_hypothetical_quote(
    conn: sqlite3.Connection,
    prediction_id: int,
    quote: HypotheticalMakerQuote,
    *,
    evidence: dict[str, Any] | None = None,
) -> int:
    if conn.execute("SELECT 1 FROM paper_maker_predictions WHERE id=?", (prediction_id,)).fetchone() is None:
        raise ValueError("prediction does not exist")
    with conn:
        cursor = conn.execute(
            """INSERT INTO paper_maker_hypothetical_quotes
            (prediction_id,market_id,token_id,side,quote_price,quote_size_shares,
             signaled_at_utc,eligible_from_utc,displayed_depth_ahead_shares,
             displayed_depth_at_quote_shares,evidence_json)
             VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                prediction_id,
                quote.market_id,
                quote.token_id,
                quote.side,
                quote.price,
                quote.size_shares,
                quote.signaled_at_utc,
                quote.eligible_from_utc,
                quote.displayed_depth_ahead_shares,
                quote.displayed_depth_at_quote_shares,
                _json(evidence or {}),
            ),
        )
    return int(cursor.lastrowid)
