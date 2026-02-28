# live_runner.py
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any, Dict, List

def _table_exists(conn, name: str) -> bool:
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1;", (name,))
    return cur.fetchone() is not None

from risk.risk_engine import RiskConfig, compute_size_usd, passes_filters, side_from_p_yes
from paper.paper_ledger import PaperTrade, ensure_paper_tables, insert_paper_trade

DB_PATH = os.path.join("memory", "runs.sqlite")
SIGNALS_PATH = os.path.join("signals", "trade_candidates.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def kill_switch_present(kill_file: str) -> bool:
    return os.path.exists(kill_file)


def run_orchestrator() -> None:
    # no edits to orchestrator; just execute it
    subprocess.check_call(["python3", "orchestrator.py"])


def fetch_latest_run_bundle(conn: sqlite3.Connection) -> Dict[str, Any]:
    cur = conn.cursor()

    cur.execute("SELECT run_id, ts_utc, market_id, question, outcome FROM runs ORDER BY id DESC LIMIT 1;")
    r = cur.fetchone()
    if not r:
        raise RuntimeError("No rows in runs table yet.")
    run_id, ts_utc, market_id, question, outcome = r

    cur.execute("""
      SELECT consensus_side, consensus_p_yes, disagreement, winner_agent, winner_fitness, notes
      FROM arbiter_runs
      WHERE run_id = ?
      ORDER BY id DESC LIMIT 1;
    """, (run_id,))
    a = cur.fetchone()
    if not a:
        arbiter = None
    # Fallback: proceed without arbiter row (use runs.operator_* and runs.skeptic_* fields)
    return {
        "run_id": run_id,
        "run": run_row,
        "arbiter": arbiter,
    }
    consensus_side, consensus_p_yes, disagreement, winner_agent, winner_fitness, notes = a

    return {
        "run_id": run_id,
        "ts_utc": ts_utc,
        "market_id": market_id,
        "question": question,
        "outcome": outcome,
        "arbiter": {
            "consensus_side": consensus_side,
            "consensus_p_yes": float(consensus_p_yes),
            "disagreement": float(disagreement),
            "winner_agent": winner_agent,
            "winner_fitness": float(winner_fitness),
            "notes": notes,
        }
    }


def write_signals(payload: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(SIGNALS_PATH), exist_ok=True)
    with open(SIGNALS_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="fake", choices=["fake", "polymarket"], help="Market source (ingestion step).")
    ap.add_argument("--paper", action="store_true", help="Write paper trades to DB (no real trading).")
    ap.add_argument("--bankroll", type=float, default=float(os.getenv("BANKROLL_USD", "1000")))
    ap.add_argument("--max_per_trade_pct", type=float, default=float(os.getenv("MAX_PER_TRADE_PCT", "0.02")))
    ap.add_argument("--min_edge", type=float, default=float(os.getenv("MIN_EDGE", "0.06")))
    ap.add_argument("--min_conf", type=float, default=float(os.getenv("MIN_CONF", "0.52")))
    ap.add_argument("--max_daily_trades", type=int, default=int(os.getenv("MAX_DAILY_TRADES", "5")))
    args = ap.parse_args()

    cfg = RiskConfig(
        bankroll_usd=args.bankroll,
        max_per_trade_pct=args.max_per_trade_pct,
        max_daily_trades=args.max_daily_trades,
        min_edge=args.min_edge,
        min_confidence=args.min_conf,
    )

    if kill_switch_present(cfg.hard_kill_file):
        print(f"KILL switch present ({cfg.hard_kill_file}). Exiting.")
        return

    # Step 1: run sim engine (writes runs/agent_runs/arbiter_runs)
    run_orchestrator()

    # Step 2: load latest bundle
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    ensure_paper_tables(conn)
    bundle = fetch_latest_run_bundle(conn)
    run = bundle["run"]
    arb = bundle.get("arbiter")

    # Derive consensus if arbiter row is missing
    if arb is None:
        op_side = run["operator_side"]
        op_conf = float(run["operator_conf"])
        sk_side = run["skeptic_side"]
        sk_conf = float(run["skeptic_conf"])

        p_yes_op = op_conf if op_side == "YES" else (1.0 - op_conf)
        p_yes_sk = sk_conf if sk_side == "YES" else (1.0 - sk_conf)

        consensus_p_yes = (p_yes_op + p_yes_sk) / 2.0
        disagreement = abs(p_yes_op - p_yes_sk)
        consensus_side = "YES" if consensus_p_yes >= 0.5 else "NO"
    else:
        consensus_side = arb["consensus_side"]
        consensus_p_yes = float(arb["consensus_p_yes"])
        disagreement = float(arb["disagreement"])

    run = bundle["run"]
    arb = bundle.get("arbiter")

    # Derive consensus if arbiter row is missing
    if arb is None:
        op_side = run["operator_side"]
        op_conf = float(run["operator_conf"])
        sk_side = run["skeptic_side"]
        sk_conf = float(run["skeptic_conf"])

        p_yes_op = op_conf if op_side == "YES" else (1.0 - op_conf)
        p_yes_sk = sk_conf if sk_side == "YES" else (1.0 - sk_conf)

        consensus_p_yes = (p_yes_op + p_yes_sk) / 2.0
        disagreement = abs(p_yes_op - p_yes_sk)
        consensus_side = "YES" if consensus_p_yes >= 0.5 else "NO"
    else:
        consensus_side = arb["consensus_side"]
        consensus_p_yes = float(arb["consensus_p_yes"])
        disagreement = float(arb["disagreement"])


    p_yes = consensus_p_yes
    disagreement = disagreement

    ok, reason = passes_filters(cfg, p_yes, disagreement)
    side = side_from_p_yes(p_yes)
    size = compute_size_usd(cfg, p_yes, disagreement) if ok else 0.0

    signal = {
        "ts_utc": utc_now_iso(),
        "source": args.source,
        "run_id": bundle["run_id"],
        "market_id": bundle["market_id"],
        "question": bundle["question"],
        "arbiter": bundle["arbiter"],
        "decision": {
            "side": side,
            "eligible": bool(ok),
            "filter_reason": reason,
            "size_usd": size,
        },
        "notes": {
            "warning": "Paper-only by default. Live execution not implemented yet.",
        }
    }

    write_signals(signal)

    # Step 3: paper ledger insert
    if args.paper and ok and size > 0:
        t = PaperTrade(
            run_id=bundle["run_id"],
            ts_utc=utc_now_iso(),
            market_id=bundle["market_id"],
            question=bundle["question"],
            venue=args.source,
            side=side,
            consensus_p_yes=p_yes,
            disagreement=disagreement,
            size_usd=size,
            reason=reason,
            status="OPEN",
            resolved_outcome=bundle["outcome"] if bundle["outcome"] in ("YES", "NO") else None,
        )
        insert_paper_trade(conn, t)

    conn.close()

    # Step 4: update report
    subprocess.check_call(["python3", "reporting/eval_live.py"])

    print(f"Wrote {SIGNALS_PATH}")


if __name__ == "__main__":
    main()
