from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from .model import BasketEvaluation


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS paper_negrisk_predictions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  record_key TEXT NOT NULL UNIQUE,
  recorded_at_utc TEXT NOT NULL,
  event_id TEXT NOT NULL,
  execution_mode TEXT NOT NULL CHECK (execution_mode='PAPER_ONLY'),
  status TEXT NOT NULL,
  economic_status TEXT NOT NULL,
  requested_shares REAL NOT NULL,
  paper_filled_shares REAL NOT NULL,
  gross_edge_usd REAL,
  slippage_usd REAL,
  fee_usd REAL,
  predicted_net_edge_usd REAL,
  fill_probability REAL,
  fill_probability_status TEXT NOT NULL,
  prediction_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS paper_negrisk_outcomes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  prediction_id INTEGER NOT NULL UNIQUE REFERENCES paper_negrisk_predictions(id),
  observed_at_utc TEXT NOT NULL,
  outcome_source TEXT NOT NULL,
  complete_basket_payout_usd REAL,
  realized_net_after_modeled_costs_usd REAL,
  prediction_error_usd REAL,
  metadata_json TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


def initialize_paper_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def record_prediction(conn: sqlite3.Connection, evaluation: BasketEvaluation, *, recorded_at_utc: str | None = None) -> int:
    if evaluation.execution_mode != "PAPER_ONLY":
        raise ValueError("only PAPER_ONLY evaluations can be recorded")
    payload = evaluation.as_dict()
    encoded = _json(payload)
    timestamp = recorded_at_utc or _now()
    key = hashlib.sha256(f"{timestamp}:{encoded}".encode()).hexdigest()
    with conn:
        cursor = conn.execute(
            """INSERT INTO paper_negrisk_predictions
            (record_key,recorded_at_utc,event_id,execution_mode,status,economic_status,
             requested_shares,paper_filled_shares,gross_edge_usd,slippage_usd,fee_usd,
             predicted_net_edge_usd,fill_probability,fill_probability_status,prediction_json)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                key,
                timestamp,
                evaluation.event_id,
                evaluation.execution_mode,
                evaluation.status,
                evaluation.economic_status,
                evaluation.requested_shares,
                evaluation.paper_filled_shares,
                evaluation.gross_edge_usd,
                evaluation.slippage_usd,
                evaluation.fee_usd,
                evaluation.net_executable_edge_usd,
                evaluation.fill_probability,
                evaluation.fill_probability_status,
                encoded,
            ),
        )
    return int(cursor.lastrowid)


def record_outcome(
    conn: sqlite3.Connection,
    prediction_id: int,
    *,
    observed_at_utc: str,
    outcome_source: str,
    complete_basket_payout_usd: float | None,
    metadata: dict[str, Any] | None = None,
) -> None:
    row = conn.execute(
        "SELECT paper_filled_shares,predicted_net_edge_usd,prediction_json FROM paper_negrisk_predictions WHERE id=?",
        (prediction_id,),
    ).fetchone()
    if row is None:
        raise ValueError("prediction does not exist")
    predicted = row[1]
    realized = error = None
    if complete_basket_payout_usd is not None and predicted is not None:
        payload = json.loads(row[2])
        realized = float(complete_basket_payout_usd) - float(payload["walked_cost_usd"]) - float(payload["fee_usd"])
        error = realized - float(predicted)
    with conn:
        conn.execute(
            """INSERT INTO paper_negrisk_outcomes
            (prediction_id,observed_at_utc,outcome_source,complete_basket_payout_usd,
             realized_net_after_modeled_costs_usd,prediction_error_usd,metadata_json)
            VALUES(?,?,?,?,?,?,?)""",
            (prediction_id, observed_at_utc, outcome_source, complete_basket_payout_usd, realized, error, _json(metadata or {})),
        )


def compare_predictions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT p.id,p.event_id,p.status,p.gross_edge_usd,p.slippage_usd,p.fee_usd,
                  p.predicted_net_edge_usd,p.fill_probability_status,o.observed_at_utc,
                  o.complete_basket_payout_usd,o.realized_net_after_modeled_costs_usd,
                  o.prediction_error_usd
           FROM paper_negrisk_predictions p
           LEFT JOIN paper_negrisk_outcomes o ON o.prediction_id=p.id
           ORDER BY p.id"""
    ).fetchall()
    return [dict(row) for row in rows]
