from __future__ import annotations

import json
import os
import sqlite3
import uuid
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

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


def ensure_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        ts_utc TEXT NOT NULL,
        market_id TEXT NOT NULL,
        question TEXT NOT NULL,
        outcome TEXT NOT NULL,

        operator_side TEXT NOT NULL,
        operator_conf REAL NOT NULL,
        operator_rationale TEXT NOT NULL,

        skeptic_side TEXT NOT NULL,
        skeptic_conf REAL NOT NULL,
        skeptic_rationale TEXT NOT NULL,

        operator_score REAL NOT NULL,
        skeptic_score REAL NOT NULL,

        operator_notes TEXT NOT NULL,
        skeptic_notes TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_market ON runs(market_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_runid ON runs(run_id);")
    conn.commit()
    conn.close()


def migrate_db_add_run_id_if_missing() -> int:
    """
    If the DB pre-exists from v0, add run_id column and backfill.
    Returns number of rows backfilled.
    """
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("PRAGMA table_info(runs);")
    cols = [r[1] for r in cur.fetchall()]
    if "run_id" not in cols:
        cur.execute("ALTER TABLE runs ADD COLUMN run_id TEXT;")
        conn.commit()

    cur.execute("SELECT id FROM runs WHERE run_id IS NULL OR run_id = '';")
    ids = [r[0] for r in cur.fetchall()]
    for rid in ids:
        cur.execute("UPDATE runs SET run_id = ? WHERE id = ?;", (str(uuid.uuid4()), rid))
    conn.commit()
    conn.close()
    return len(ids)


def get_agent_last_n_accuracy(conn: sqlite3.Connection, agent: str, n: int) -> Optional[float]:
    cur = conn.cursor()
    if agent == "operator":
        cur.execute("""
        SELECT AVG(CASE WHEN operator_side = outcome THEN 1.0 ELSE 0.0 END)
        FROM (SELECT operator_side, outcome FROM runs ORDER BY id DESC LIMIT ?);
        """, (n,))
    elif agent == "skeptic":
        cur.execute("""
        SELECT AVG(CASE WHEN skeptic_side = outcome THEN 1.0 ELSE 0.0 END)
        FROM (SELECT skeptic_side, outcome FROM runs ORDER BY id DESC LIMIT ?);
        """, (n,))
    else:
        raise ValueError("agent must be 'operator' or 'skeptic'")
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def _extract_reward(notes: str) -> float | None:
    # notes contains: '... Reward=0.867. ...'
    m = re.search(r"Reward=([0-9]+\.[0-9]+)", notes)
    return float(m.group(1)) if m else None

def get_agent_last_n_avg_reward(conn: sqlite3.Connection, agent: str, n: int) -> Optional[float]:
    cur = conn.cursor()
    if agent == "operator":
        cur.execute("SELECT AVG(operator_reward) FROM (SELECT operator_reward FROM runs ORDER BY id DESC LIMIT ?);", (n,))
    elif agent == "skeptic":
        cur.execute("SELECT AVG(skeptic_reward) FROM (SELECT skeptic_reward FROM runs ORDER BY id DESC LIMIT ?);", (n,))
    else:
        raise ValueError("agent must be 'operator' or 'skeptic'")
    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def get_agent_rolling_accuracy(conn: sqlite3.Connection, agent: str) -> Optional[float]:
    """
    Rolling accuracy for a specific agent: 'operator' or 'skeptic'.
    Returns None if no history.
    """
    cur = conn.cursor()
    if agent == "operator":
        cur.execute("SELECT AVG(CASE WHEN operator_side = outcome THEN 1.0 ELSE 0.0 END) FROM runs;")
    elif agent == "skeptic":
        cur.execute("SELECT AVG(CASE WHEN skeptic_side = outcome THEN 1.0 ELSE 0.0 END) FROM runs;")
    else:
        raise ValueError("agent must be 'operator' or 'skeptic'")

    row = cur.fetchone()
    if not row or row[0] is None:
        return None
    return float(row[0])


def load_agent_state() -> Dict[str, Any]:
    if not AGENT_STATE_PATH.exists():
        return {"operator": {"mode": "heuristic_yes_bias", "seed": 1337}, "skeptic": {"mode": "always_opposite", "seed": 1337}}
    return json.loads(AGENT_STATE_PATH.read_text(encoding="utf-8"))

def save_agent_state(state: Dict[str, Any]) -> None:
    AGENT_STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")

def load_markets() -> list[Dict[str, Any]]:
    if not MARKETS_PATH.exists():
        raise FileNotFoundError(f"Missing {MARKETS_PATH}. Create markets/fake_markets.json first.")
    return json.loads(MARKETS_PATH.read_text(encoding="utf-8"))


def pick_market_round_robin(markets: list[Dict[str, Any]]) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM runs;")
    run_count = int(cur.fetchone()[0] or 0)
    conn.close()
    return markets[run_count % len(markets)]


def log_run(payload: Dict[str, Any]) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
    # microsecond precision to avoid collisions in tight loops
    ts = datetime.now(timezone.utc)
    fname = f"run_{ts.strftime('%Y%m%dT%H%M%S')}_{ts.microsecond:06d}Z.json"
    path = os.path.join(LOG_DIR, fname)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
    return path

def append_event(event: Dict[str, Any]) -> None:
    os.makedirs(LOG_DIR, exist_ok=True)
    events_path = os.path.join(LOG_DIR, "events.jsonl")
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True) + "\n")



def main() -> None:
    ensure_db()
    backfilled = migrate_db_add_run_id_if_missing()

    markets = load_markets()
    market = pick_market_round_robin(markets)

    agent_state = load_agent_state()
    operator = run_operator(market, agent_state.get('operator'))
    skeptic = run_skeptic(
        market,
        operator_side=operator.side,
        operator_conf=operator.confidence,
        operator_rationale=operator.rationale,
    )

    conn = sqlite3.connect(DB_PATH)
    rolling_op = get_agent_rolling_accuracy(conn, "operator")
    rolling_sk = get_agent_rolling_accuracy(conn, "skeptic")

    # v0.3: last-N avg reward (from notes) for Reaper decisions
    N = 10
    THRESH_REWARD = 0.45
    last_op_reward = get_agent_last_n_avg_reward(conn, 'operator', N)
    last_sk_reward = get_agent_last_n_avg_reward(conn, 'skeptic', N)
    reap_events = []

    op_score = score_prediction(operator.side, operator.confidence, market["outcome"], rolling_op)
    sk_score = score_prediction(skeptic.side, skeptic.confidence, market["outcome"], rolling_sk)

    # Reaper v0: reset agent if last-N accuracy dips too low
    N = 10
    THRESH = 0.45
    last_op = get_agent_last_n_accuracy(conn, 'operator', N)
    last_sk = get_agent_last_n_accuracy(conn, 'skeptic', N)
    reap_events = []
    ev1 = maybe_reap(agent_state, 'operator', last_op_reward, N, THRESH_REWARD)
    if ev1: reap_events.append(ev1.__dict__)
    ev2 = maybe_reap(agent_state, 'skeptic', last_sk_reward, N, THRESH_REWARD)
    if ev2: reap_events.append(ev2.__dict__)
    if reap_events:
        save_agent_state(agent_state)

    ts = utc_now_iso()
    run_id = str(uuid.uuid4())

    row = (
        run_id,
        ts,
        market["market_id"],
        market["question"],
        market["outcome"],
        operator.side,
        operator.confidence,
        operator.rationale,
        skeptic.side,
        skeptic.confidence,
        skeptic.rationale,
        op_score.total_score,
        sk_score.total_score,
        op_score.brier,
        op_score.reward,
        sk_score.brier,
        sk_score.reward,
        op_score.notes,
        sk_score.notes,
    )

    cur = conn.cursor()
    cur.execute("""
    INSERT INTO runs (
        run_id, ts_utc, market_id, question, outcome,
        operator_side, operator_conf, operator_rationale,
        skeptic_side, skeptic_conf, skeptic_rationale,
        operator_score, skeptic_score,
        operator_brier, operator_reward,
        skeptic_brier, skeptic_reward,
        operator_notes, skeptic_notes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, row)
    conn.commit()
    conn.close()

    # v0.3 safety: ensure last-N reward vars exist even if reaper block is skipped

    last_op_reward = locals().get('last_op_reward', None)

    last_sk_reward = locals().get('last_sk_reward', None)

    reap_events = locals().get('reap_events', [])


    payload = {
        "run_id": run_id,
        "ts_utc": ts,
        "db_backfilled_run_id_rows": backfilled,
        "market": market,
        "rolling_accuracy_before": {"operator": rolling_op, "skeptic": rolling_sk},
        "last_n_avg_reward_before": {"n": 10, "operator": last_op_reward, "skeptic": last_sk_reward},
        "reaper_events": reap_events,
        "last_n_accuracy_before": {"n": 10, "operator": last_op, "skeptic": last_sk},
        "reaper_events": reap_events,
        "operator": {
            "side": operator.side,
            "confidence": operator.confidence,
            "rationale": operator.rationale,
            "score": op_score.__dict__,
        },
        "skeptic": {
            "side": skeptic.side,
            "confidence": skeptic.confidence,
            "rationale": skeptic.rationale,
            "score": sk_score.__dict__,
        },
    }

    log_path = log_run(payload)
    if reap_events:
        for ev in reap_events:
            append_event({"ts_utc": ts, "run_id": run_id, "type": "REAPER_EVENT", **ev})

    print("BLACK GLASS LAB v0.4 — run complete")
    print(f"- Run ID: {run_id}")
    print(f"- DB:  {DB_PATH}")
    print(f"- Log: {log_path}")
    print(f"- Outcome: {market['outcome']}")
    print(f"- RollingAcc: operator={rolling_op} skeptic={rolling_sk}")
    print(f"- Operator: {operator.side} ({operator.confidence:.2f}) score={op_score.total_score:.3f}")
    print(f"- Skeptic:  {skeptic.side} ({skeptic.confidence:.2f}) score={sk_score.total_score:.3f}")


if __name__ == "__main__":
    main()
