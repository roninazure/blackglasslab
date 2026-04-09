from __future__ import annotations

def _bgl_single_market_override() -> dict | None:
    """
    Optional override: run swarm on exactly one market.
    Env:
      BGL_MARKET_ID=<slug>
      BGL_MARKET_QUESTION=<question>   (optional; if missing, we try to fetch)
    """
    mid = os.environ.get("BGL_MARKET_ID")
    if not mid:
        return None
    q = os.environ.get("BGL_MARKET_QUESTION", "").strip()
    if not q:
        try:
            from adapters import get_adapter
            a = get_adapter("polymarket")
            m = a.get_market(mid)
            q = (m.get("question") or mid).strip()
        except Exception:
            q = mid
    # outcome is required by schema; for forecast jobs it's not used for "truth"
    # We set a placeholder to keep schema happy. Do NOT treat as ground truth.
    return {"market_id": mid, "question": q, "outcome": "UNRESOLVED"}



import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from agents.operator import run as run_operator
from agents.skeptic import run as run_skeptic
from agents.auditor import score_prediction
from agents.reaper import maybe_reap
from agents.evolver import ensure_seed_population, sample_active, evolve, PopMember


DB_PATH = os.path.join("memory", "runs.sqlite")
LOG_DIR = "logs"
MARKETS_PATH = Path("markets") / "fake_markets.json"
AGENT_STATE_PATH = Path("agent_state.json")  # kept for backward compatibility


# Swarm sizes (per run)
K_OP = 3
K_SK = 3

# Evolution cadence
EVAL_INTERVAL = 50  # every N runs
EVOLVE_WINDOW = 600
EVOLVE_MIN_OBS = 10
EVOLVE_KEEP = 8
EVOLVE_SPAWN = 8


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # runs table (v0.5+)
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
        skeptic_notes TEXT NOT NULL,

        operator_brier REAL NOT NULL DEFAULT 0.0,
        operator_reward REAL NOT NULL DEFAULT 0.0,
        skeptic_brier REAL NOT NULL DEFAULT 0.0,
        skeptic_reward REAL NOT NULL DEFAULT 0.0
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_market ON runs(market_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_runs_runid ON runs(run_id);")

    # agent_runs must exist (you already have it)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      agent_name TEXT NOT NULL,
      side TEXT NOT NULL,
      conf REAL NOT NULL,
      rationale TEXT NOT NULL,
      brier REAL NOT NULL,
      reward REAL NOT NULL,
      score REAL NOT NULL,
      notes TEXT NOT NULL,
      ts_utc TEXT NOT NULL,
      fitness REAL NOT NULL DEFAULT 0.0,
      agent_id TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_runid ON agent_runs(run_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_runid_agent ON agent_runs(run_id, agent_name);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_fitness ON agent_runs(fitness);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_id ON agent_runs(agent_id);")    # agent_population table (required by agents.evolver.ensure_seed_population)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS agent_population (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      agent_id TEXT NOT NULL UNIQUE,
      role TEXT NOT NULL,
      mode TEXT NOT NULL,
      seed INTEGER NOT NULL,
      generation INTEGER NOT NULL DEFAULT 0,
      parent_agent_id TEXT,
      mutation TEXT NOT NULL DEFAULT '{}',
      is_active INTEGER NOT NULL DEFAULT 1,
      fitness REAL NOT NULL DEFAULT 0.0,
      notes TEXT NOT NULL DEFAULT '{}',
      created_ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
      updated_ts_utc TEXT NOT NULL DEFAULT (datetime('now')),
      last_used_ts_utc TEXT
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_population_role ON agent_population(role);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_population_active ON agent_population(is_active);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_population_role_active ON agent_population(role, is_active);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_population_generation ON agent_population(generation);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_agent_population_agent_id ON agent_population(agent_id);")


    # paper_trades table (used by live_runner --paper)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS paper_trades (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      ts_utc TEXT NOT NULL,
      market_id TEXT NOT NULL,
      question TEXT NOT NULL,
      venue TEXT NOT NULL,
      side TEXT NOT NULL,
      consensus_p_yes REAL NOT NULL,
      disagreement REAL NOT NULL,
      size_usd REAL NOT NULL,
      reason TEXT NOT NULL,
      status TEXT NOT NULL,
      resolved_outcome TEXT,
      p_yes REAL NOT NULL,
      edge REAL NOT NULL,
      brier REAL,
      notes TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_run_id ON paper_trades(run_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_market_id ON paper_trades(market_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);")

    # arbiter_runs table (v0.6+) — used by live_runner consensus mode
    cur.execute("""
    CREATE TABLE IF NOT EXISTS arbiter_runs (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id TEXT NOT NULL,
      ts_utc TEXT NOT NULL,
      consensus_side TEXT NOT NULL,
      consensus_p_yes REAL NOT NULL,
      disagreement REAL NOT NULL,
      winner_agent TEXT NOT NULL,
      winner_fitness REAL NOT NULL,
      notes TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_arbiter_runs_runid ON arbiter_runs(run_id);")

    conn.commit()
    conn.close()


def load_markets() -> List[Dict[str, Any]]:
    ov = _bgl_single_market_override()
    if ov is not None:
        return [ov]

    if not MARKETS_PATH.exists():
        raise FileNotFoundError(f"Missing {MARKETS_PATH}. Create markets/fake_markets.json first.")
    return json.loads(MARKETS_PATH.read_text(encoding="utf-8"))


def pick_market_round_robin(markets: List[Dict[str, Any]]) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM runs;")
    run_count = int(cur.fetchone()[0] or 0)
    conn.close()
    return markets[run_count % len(markets)]


def log_run(payload: Dict[str, Any]) -> str:
    os.makedirs(LOG_DIR, exist_ok=True)
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


def insert_agent_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    agent_id: str | None,
    agent_name: str,
    side: str,
    conf: float,
    rationale: str,
    brier: float,
    reward: float,
    score: float,
    notes: str,
    fitness: float,
    ts_utc: str,
) -> None:
    conn.execute(
        """
        INSERT INTO agent_runs (
          run_id, agent_id, agent_name, side, conf, rationale,
          brier, reward, score, notes, fitness, ts_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (run_id, agent_id, agent_name, side, float(conf), rationale, float(brier),
         float(reward), float(score), notes, float(fitness), ts_utc),
    )


def _p_yes(side: str, conf: float) -> float:
    s = (side or "").strip().upper()
    c = float(conf)
    return c if s == "YES" else (1.0 - c)


def _compute_arbiter_from_swarm(op_results, sk_results) -> Dict[str, Any]:
    """
    Stable inline arbiter:
    - consensus_p_yes: mean P(YES) across all agents in this run
    - disagreement: range(max-min) P(YES) across all agents
    - winner: max fitness across all agents
    """
    points: List[float] = []
    winner_agent = "unknown"
    winner_fitness = -1e18

    for (m, out, sc, fit) in (op_results or []):
        py = _p_yes(out.side, out.confidence)
        points.append(py)
        if float(fit) > winner_fitness:
            winner_fitness = float(fit)
            winner_agent = f"operator_{m.mode}"

    for (m, out, sc, fit) in (sk_results or []):
        py = _p_yes(out.side, out.confidence)
        points.append(py)
        if float(fit) > winner_fitness:
            winner_fitness = float(fit)
            winner_agent = f"skeptic_{m.mode}"

    if not points:
        consensus_p_yes = 0.5
        disagreement = 0.0
    else:
        consensus_p_yes = sum(points) / float(len(points))
        disagreement = max(points) - min(points)

    consensus_side = "YES" if consensus_p_yes >= 0.5 else "NO"
    notes = f"arbiter=v0.7_inline mean_p_yes={consensus_p_yes:.6f} range={disagreement:.6f} n={len(points)}"

    return {
        "consensus_side": consensus_side,
        "consensus_p_yes": float(consensus_p_yes),
        "disagreement": float(disagreement),
        "winner_agent": winner_agent,
        "winner_fitness": float(winner_fitness if winner_fitness > -1e17 else 0.0),
        "notes": notes,
    }


def insert_arbiter_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    ts_utc: str,
    consensus_side: str,
    consensus_p_yes: float,
    disagreement: float,
    winner_agent: str,
    winner_fitness: float,
    notes: str,
) -> None:
    conn.execute(
        """
        INSERT INTO arbiter_runs (
          run_id, ts_utc, consensus_side, consensus_p_yes, disagreement,
          winner_agent, winner_fitness, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            run_id,
            ts_utc,
            consensus_side,
            float(consensus_p_yes),
            float(disagreement),
            winner_agent,
            float(winner_fitness),
            notes,
        ),
    )


def maybe_evolve(conn: sqlite3.Connection, ts: str) -> List[Dict[str, Any]]:
    """
    Run evolution periodically based on number of completed runs.
    """
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM runs;")
    run_count = int(cur.fetchone()[0] or 0)

    if run_count == 0 or (run_count % EVAL_INTERVAL) != 0:
        return []

    evs: List[Dict[str, Any]] = []
    for role in ("operator", "skeptic"):
        res = evolve(
            conn,
            role=role,
            window_agent_runs=EVOLVE_WINDOW,
            min_obs=EVOLVE_MIN_OBS,
            keep_top=EVOLVE_KEEP,
            spawn=EVOLVE_SPAWN,
        )
        evs.append({"type": "EVOLVE_EVENT", "ts_utc": ts, "run_count": run_count, **res})
    return evs


def main() -> None:
    ensure_db()
    markets = load_markets()
    market = pick_market_round_robin(markets)

    ts = utc_now_iso()
    run_id = str(uuid.uuid4())

    conn = sqlite3.connect(DB_PATH)
    ensure_seed_population(conn, per_role=12)

    # Sample a swarm from active population
    seed_int = int(uuid.uuid4().int & 0xFFFFFFFF)

    op_members: List[PopMember] = sample_active(conn, "operator", K_OP, seed=seed_int)
    sk_members: List[PopMember] = sample_active(conn, "skeptic", K_SK, seed=seed_int ^ 0xA5A5A5A5)

    # Rolling accuracy (optional influence in scoring)
    cur = conn.cursor()
    cur.execute("SELECT AVG(CASE WHEN operator_side = outcome THEN 1.0 ELSE 0.0 END) FROM runs;")
    rolling_op = cur.fetchone()[0]
    rolling_op = float(rolling_op) if rolling_op is not None else None

    cur.execute("SELECT AVG(CASE WHEN skeptic_side = outcome THEN 1.0 ELSE 0.0 END) FROM runs;")
    rolling_sk = cur.fetchone()[0]
    rolling_sk = float(rolling_sk) if rolling_sk is not None else None

    # Run operator swarm first
    op_results = []
    for i, m in enumerate(op_members):
        state = {"mode": m.mode, "seed": m.seed}
        out = run_operator(market, state)
        score = score_prediction(out.side, out.confidence, market["outcome"], rolling_op)
        # fitness = reward - brier (simple, stable, bounded-ish)
        fitness = float(score.reward) - float(score.brier)
        op_results.append((m, out, score, fitness))
        insert_agent_run(
            conn,
            run_id=run_id,
            agent_id=m.agent_id,
            agent_name=f"operator_{m.mode}",
            side=out.side,
            conf=out.confidence,
            rationale=out.rationale,
            brier=score.brier,
            reward=score.reward,
            score=score.total_score,
            notes=score.notes,
            fitness=fitness,
            ts_utc=ts,
        )

    # Pick a representative operator output for legacy runs table fields:
    # choose highest fitness operator in this run
    op_best = max(op_results, key=lambda t: t[3])

    # Run skeptic swarm (each skeptic sees the chosen operator signal)
    sk_results = []
    for i, m in enumerate(sk_members):
        state = {"mode": m.mode, "seed": m.seed}
        out = run_skeptic(
            market,
            operator_side=op_best[1].side,
            operator_conf=op_best[1].confidence,
            operator_rationale=op_best[1].rationale,
            skeptic_state=state,
        )
        score = score_prediction(out.side, out.confidence, market["outcome"], rolling_sk)
        fitness = float(score.reward) - float(score.brier)
        sk_results.append((m, out, score, fitness))
        insert_agent_run(
            conn,
            run_id=run_id,
            agent_id=m.agent_id,
            agent_name=f"skeptic_{m.mode}",
            side=out.side,
            conf=out.confidence,
            rationale=out.rationale,
            brier=score.brier,
            reward=score.reward,
            score=score.total_score,
            notes=score.notes,
            fitness=fitness,
            ts_utc=ts,
        )

    sk_best = max(sk_results, key=lambda t: t[3])

    # Reaper can still exist (legacy evolutionary pressure)
    # We keep it but treat it as "agent_state mutation" separate from population.
    agent_state = {"operator": {"mode": op_best[0].mode, "seed": op_best[0].seed},
                   "skeptic": {"mode": sk_best[0].mode, "seed": sk_best[0].seed}}
    # Use last-10 avg reward from runs table fields (if present)
    N = 10
    THRESH_REWARD = 0.45
    cur.execute("SELECT AVG(operator_reward) FROM (SELECT operator_reward FROM runs ORDER BY id DESC LIMIT ?);", (N,))
    last_op_reward = cur.fetchone()[0]
    last_op_reward = float(last_op_reward) if last_op_reward is not None else None
    cur.execute("SELECT AVG(skeptic_reward) FROM (SELECT skeptic_reward FROM runs ORDER BY id DESC LIMIT ?);", (N,))
    last_sk_reward = cur.fetchone()[0]
    last_sk_reward = float(last_sk_reward) if last_sk_reward is not None else None

    reap_events = []
    ev1 = maybe_reap(agent_state, "operator", last_op_reward, N, THRESH_REWARD)
    if ev1:
        reap_events.append(ev1.__dict__)
    ev2 = maybe_reap(agent_state, "skeptic", last_sk_reward, N, THRESH_REWARD)
    if ev2:
        reap_events.append(ev2.__dict__)
    if reap_events:
        # persist for forensics only
        AGENT_STATE_PATH.write_text(json.dumps(agent_state, indent=2, sort_keys=True), encoding="utf-8")
        for ev in reap_events:
            append_event({"ts_utc": ts, "run_id": run_id, "type": "REAPER_EVENT", **ev})

    # Insert legacy 'runs' row (best-op + best-sk as representative)
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
            run_id, ts, market["market_id"], market["question"], market["outcome"],
            op_best[1].side, float(op_best[1].confidence), op_best[1].rationale,
            sk_best[1].side, float(sk_best[1].confidence), sk_best[1].rationale,
            float(op_best[2].total_score), float(sk_best[2].total_score),
            float(op_best[2].brier), float(op_best[2].reward),
            float(sk_best[2].brier), float(sk_best[2].reward),
            op_best[2].notes, sk_best[2].notes,
        ),
    )

    # Write arbiter consensus row (enables live_runner arbiter mode)
    arb = _compute_arbiter_from_swarm(op_results, sk_results)
    insert_arbiter_run(
        conn,
        run_id=run_id,
        ts_utc=ts,
        consensus_side=arb["consensus_side"],
        consensus_p_yes=arb["consensus_p_yes"],
        disagreement=arb["disagreement"],
        winner_agent=arb["winner_agent"],
        winner_fitness=arb["winner_fitness"],
        notes=arb["notes"],
    )

    # Evolution step every EVAL_INTERVAL runs
    evolve_events = maybe_evolve(conn, ts)
    for ev in evolve_events:
        append_event(ev)

    conn.commit()
    conn.close()

    payload = {
        "run_id": run_id,
        "ts_utc": ts,
        "market": market,
        "swarm": {
            "operators": [
                {"agent_id": m.agent_id, "mode": m.mode, "seed": m.seed, "side": out.side, "conf": out.confidence, "fitness": fit}
                for (m, out, sc, fit) in op_results
            ],
            "skeptics": [
                {"agent_id": m.agent_id, "mode": m.mode, "seed": m.seed, "side": out.side, "conf": out.confidence, "fitness": fit}
                for (m, out, sc, fit) in sk_results
            ],
        },
        "best": {
            "operator": {"agent_id": op_best[0].agent_id, "mode": op_best[0].mode, "fitness": op_best[3]},
            "skeptic": {"agent_id": sk_best[0].agent_id, "mode": sk_best[0].mode, "fitness": sk_best[3]},
        },
        "evolve_events": evolve_events,
        "reaper_events": reap_events,
    }

    log_path = log_run(payload)

    print("BLACK GLASS LAB v0.7 — run complete")
    print(f"- Run ID: {run_id}")
    print(f"- DB:  {DB_PATH}")
    print(f"- Log: {log_path}")
    print(f"- Outcome: {market['outcome']}")
    print(f"- Swarm: operators={len(op_results)} skeptics={len(sk_results)}")
    if evolve_events:
        print(f"- Evolution: fired {len(evolve_events)} events at interval={EVAL_INTERVAL}")


if __name__ == "__main__":
    main()
