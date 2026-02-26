from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agents.operator import run as run_operator
from agents.skeptic import run as run_skeptic
from agents.auditor import score_prediction
from agents.reaper import maybe_reap

DB_PATH = os.path.join("memory", "runs.sqlite")
LOG_DIR = "logs"
MARKETS_PATH = Path("markets") / "fake_markets.json"
AGENT_STATE_PATH = Path("agent_state.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_db_dirs() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def load_agent_state() -> Dict[str, Any]:
    if not AGENT_STATE_PATH.exists():
        return {
            "operator": {"mode": "heuristic_yes_bias", "seed": 1337},
            "skeptic": {"mode": "always_opposite", "seed": 1337},
        }
    return json.loads(AGENT_STATE_PATH.read_text(encoding="utf-8"))


def save_agent_state(state: Dict[str, Any]) -> None:
    AGENT_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def load_markets() -> list[Dict[str, Any]]:
    if not MARKETS_PATH.exists():
        raise FileNotFoundError(f"Missing {MARKETS_PATH}")
    return json.loads(MARKETS_PATH.read_text(encoding="utf-8"))


def pick_market_round_robin(conn: sqlite3.Connection, markets: list[Dict[str, Any]]) -> Dict[str, Any]:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM runs;")
    run_count = int(cur.fetchone()[0] or 0)
    return markets[run_count % len(markets)]


def get_agent_rolling_accuracy(conn: sqlite3.Connection, agent: str) -> Optional[float]:
    cur = conn.cursor()
    if agent == "operator":
        cur.execute("SELECT AVG(CASE WHEN operator_side = outcome THEN 1.0 ELSE 0.0 END) FROM runs;")
    else:
        cur.execute("SELECT AVG(CASE WHEN skeptic_side = outcome THEN 1.0 ELSE 0.0 END) FROM runs;")

    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def get_agent_last_n_avg_reward(conn: sqlite3.Connection, agent: str, n: int) -> Optional[float]:
    cur = conn.cursor()
    if agent == "operator":
        cur.execute(
            "SELECT AVG(operator_reward) FROM (SELECT operator_reward FROM runs ORDER BY id DESC LIMIT ?);",
            (n,),
        )
    else:
        cur.execute(
            "SELECT AVG(skeptic_reward) FROM (SELECT skeptic_reward FROM runs ORDER BY id DESC LIMIT ?);",
            (n,),
        )

    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def insert_agent_run(
    conn: sqlite3.Connection,
    run_id: str,
    ts_utc: str,
    agent_name: str,
    side: str,
    conf: float,
    rationale: str,
    brier: float,
    reward: float,
    score: float,
    notes: str,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_runs (
            run_id, agent_name, side, conf,
            rationale, brier, reward, score, notes, ts_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            run_id,
            agent_name,
            side,
            float(conf),
            rationale,
            float(brier),
            float(reward),
            float(score),
            notes,
            ts_utc,
        ),
    )


def log_run(payload: Dict[str, Any]) -> str:
    ts = datetime.now(timezone.utc)
    fname = f"run_{ts.strftime('%Y%m%dT%H%M%S')}_{ts.microsecond:06d}Z.json"
    path = os.path.join(LOG_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return path


def main() -> None:
    ensure_db_dirs()

    conn = sqlite3.connect(DB_PATH)

    markets = load_markets()
    market = pick_market_round_robin(conn, markets)

    agent_state = load_agent_state()

    operator = run_operator(market, agent_state.get("operator"))
    skeptic = run_skeptic(
        market,
        operator_side=operator.side,
        operator_conf=operator.confidence,
        operator_rationale=operator.rationale,
    )

    rolling_op = get_agent_rolling_accuracy(conn, "operator")
    rolling_sk = get_agent_rolling_accuracy(conn, "skeptic")

    op_score = score_prediction(operator.side, operator.confidence, market["outcome"], rolling_op)
    sk_score = score_prediction(skeptic.side, skeptic.confidence, market["outcome"], rolling_sk)

    # Reaper logic
    N = 10
    THRESH_REWARD = 0.45
    last_op_reward = get_agent_last_n_avg_reward(conn, "operator", N)
    last_sk_reward = get_agent_last_n_avg_reward(conn, "skeptic", N)

    reap_events = []
    ev1 = maybe_reap(agent_state, "operator", last_op_reward, N, THRESH_REWARD)
    if ev1:
        reap_events.append(ev1.__dict__)
    ev2 = maybe_reap(agent_state, "skeptic", last_sk_reward, N, THRESH_REWARD)
    if ev2:
        reap_events.append(ev2.__dict__)

    if reap_events:
        save_agent_state(agent_state)

    ts = utc_now_iso()
    run_id = str(uuid.uuid4())

    # Insert into wide runs table
    conn.execute(
        """
        INSERT INTO runs (
            run_id, ts_utc, market_id, question, outcome,
            operator_side, operator_conf, operator_rationale,
            skeptic_side, skeptic_conf, skeptic_rationale,
            operator_score, skeptic_score,
            operator_brier, operator_reward,
            skeptic_brier, skeptic_reward,
            operator_notes, skeptic_notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            run_id,
            ts,
            market["market_id"],
            market["question"],
            market["outcome"],
            operator.side,
            float(operator.confidence),
            operator.rationale,
            skeptic.side,
            float(skeptic.confidence),
            skeptic.rationale,
            float(op_score.total_score),
            float(sk_score.total_score),
            float(op_score.brier),
            float(op_score.reward),
            float(sk_score.brier),
            float(sk_score.reward),
            op_score.notes,
            sk_score.notes,
        ),
    )

    # Insert normalized per-agent rows
    insert_agent_run(
        conn,
        run_id,
        ts,
        "operator",
        operator.side,
        operator.confidence,
        operator.rationale,
        op_score.brier,
        op_score.reward,
        op_score.total_score,
        op_score.notes,
    )

    insert_agent_run(
        conn,
        run_id,
        ts,
        "skeptic",
        skeptic.side,
        skeptic.confidence,
        skeptic.rationale,
        sk_score.brier,
        sk_score.reward,
        sk_score.total_score,
        sk_score.notes,
    )

    conn.commit()
    conn.close()

    payload = {
        "run_id": run_id,
        "ts_utc": ts,
        "market": market,
        "operator": op_score.__dict__,
        "skeptic": sk_score.__dict__,
        "reaper_events": reap_events,
    }

    log_path = log_run(payload)

    print("BLACK GLASS LAB v0.5 — run complete")
    print(f"- Run ID: {run_id}")
    print(f"- DB:  {DB_PATH}")
    print(f"- Log: {log_path}")
    print(f"- Outcome: {market['outcome']}")
    print(f"- Operator: {operator.side} ({operator.confidence:.2f}) score={op_score.total_score:.3f}")
    print(f"- Skeptic:  {skeptic.side} ({skeptic.confidence:.2f}) score={sk_score.total_score:.3f}")


if __name__ == "__main__":
    main()
