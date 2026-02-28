#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


DB_PATH = os.path.join("memory", "runs.sqlite")
SIGNALS_DIR = Path("signals")
CANDIDATES_PATH = SIGNALS_DIR / "trade_candidates.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


def _fetchone_dict(cur: sqlite3.Cursor) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description]
    return dict(zip(cols, row))


def write_candidates(cands: list[Dict[str, Any]]) -> None:
    """
    Always write signals/trade_candidates.json when any candidate exists.
    Atomic write: write temp file then replace.
    """
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CANDIDATES_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cands, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(CANDIDATES_PATH)


def normalize_candidate(
    *,
    cand: Dict[str, Any],
    venue: str,
) -> Dict[str, Any]:
    """
    Ensure candidate includes all required NOT NULL columns for paper_trades schema.

    Required (NOT NULL):
      run_id, ts_utc, market_id, question, venue, side, consensus_p_yes,
      disagreement, size_usd, reason, status
    Optional:
      p_yes, edge, brier, notes
    """
    out = dict(cand)

    # Required identifiers
    out["ts_utc"] = (out.get("ts_utc") or utc_now_iso())
    out["run_id"] = (out.get("run_id") or "").strip()
    out["market_id"] = (out.get("market_id") or "UNKNOWN").strip() or "UNKNOWN"

    # Required text fields
    q = (out.get("question") or "").strip()
    if not q:
        q = f"Auto question for {out['market_id']}"
    out["question"] = q

    out["venue"] = (out.get("venue") or venue).strip() or venue

    # Side
    side = (out.get("side") or "").strip().upper()
    if side not in ("YES", "NO"):
        # infer from p_yes if present, else default NO
        p_yes = out.get("p_yes")
        try:
            p = float(p_yes) if p_yes is not None else 0.0
        except Exception:
            p = 0.0
        side = "YES" if p >= 0.5 else "NO"
    out["side"] = side

    # Probabilities / metrics
    p_yes = out.get("p_yes")
    try:
        p_float = float(p_yes) if p_yes is not None else None
    except Exception:
        p_float = None
    out["p_yes"] = p_float

    # consensus_p_yes must be NOT NULL
    # If arbiter provided it, use it; else fall back to p_yes; else 0.5
    cp = out.get("consensus_p_yes", p_float if p_float is not None else 0.5)
    try:
        out["consensus_p_yes"] = float(cp)
    except Exception:
        out["consensus_p_yes"] = 0.5

    # disagreement must be NOT NULL
    d = out.get("disagreement", 0.0)
    try:
        out["disagreement"] = float(d)
    except Exception:
        out["disagreement"] = 0.0

    # size_usd must be NOT NULL
    s = out.get("size_usd", 0.0)
    try:
        out["size_usd"] = float(s)
    except Exception:
        out["size_usd"] = 0.0

    # reason must be NOT NULL
    reason = (out.get("reason") or "live_runner").strip() or "live_runner"
    out["reason"] = reason

    # status must be NOT NULL (schema default exists, but we supply explicitly)
    status = (out.get("status") or "OPEN").strip().upper() or "OPEN"
    out["status"] = status

    # Optional numeric fields (keep as float or None)
    for k in ("edge", "brier"):
        v = out.get(k)
        if v is None:
            continue
        try:
            out[k] = float(v)
        except Exception:
            out[k] = None

    # Optional notes
    notes = out.get("notes")
    if notes is not None:
        out["notes"] = str(notes)

    return out


def insert_paper_trade(conn: sqlite3.Connection, cand: Dict[str, Any]) -> None:
    """
    Insert candidate into paper_trades using existing schema (no DB changes).
    """
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO paper_trades (
          run_id, ts_utc, market_id, question, venue, side,
          consensus_p_yes, disagreement, size_usd, reason, status,
          p_yes, edge, brier, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            cand["run_id"],
            cand["ts_utc"],
            cand["market_id"],
            cand["question"],
            cand["venue"],
            cand["side"],
            float(cand["consensus_p_yes"]),
            float(cand["disagreement"]),
            float(cand["size_usd"]),
            cand["reason"],
            cand["status"],
            (float(cand["p_yes"]) if cand.get("p_yes") is not None else None),
            (float(cand["edge"]) if cand.get("edge") is not None else None),
            (float(cand["brier"]) if cand.get("brier") is not None else None),
            (cand.get("notes") if cand.get("notes") is not None else None),
        ),
    )
    conn.commit()


def _latest_run(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM runs
        ORDER BY id DESC
        LIMIT 1
        """
    )
    return _fetchone_dict(cur)


def _latest_arbiter_for_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM arbiter_runs
        WHERE run_id=?
        ORDER BY id DESC
        LIMIT 1
        """,
        (run_id,),
    )
    return _fetchone_dict(cur)


def _fake_candidate(min_edge: float, max_disagree: float, paper_size: float) -> Optional[Dict[str, Any]]:
    """
    Deterministic-ish fake source candidate generator (enough for paper wrapper acceptance tests).
    """
    # Keep these stable and always valid. Edge/disagreement chosen to pass defaults.
    p_yes = 0.464185
    edge = 0.035815
    disagree = 0.197519

    if edge < min_edge:
        return None
    if disagree > max_disagree:
        return None

    side = "YES" if p_yes >= 0.5 else "NO"
    return {
        "ts_utc": utc_now_iso(),
        "run_id": str(os.environ.get("BGL_RUN_ID") or "").strip() or f"fallback-{utc_now_iso()}",
        "market_id": "FAKE-004",
        "question": "Will a widely used open-source library ship a breaking change without a major version bump?",
        "side": side,
        "p_yes": round(p_yes, 6),
        "edge": round(edge, 6),
        "disagreement": round(disagree, 6),
        "size_usd": float(paper_size),
        "reason": "fake_source",
        # consensus_p_yes + venue will be normalized later
    }


def build_candidate_from_db(
    *,
    run: Dict[str, Any],
    arb: Optional[Dict[str, Any]],
    min_edge: float,
    max_disagree: float,
    paper_size: float,
    venue: str,
) -> Optional[Dict[str, Any]]:
    """
    Build a trade candidate based on latest run + arbiter consensus if present.
    Falls back cleanly if arbiter_runs missing.
    """
    run_id = (run.get("run_id") or "").strip()
    market_id = (run.get("market_id") or "UNKNOWN").strip() or "UNKNOWN"
    question = (run.get("question") or "").strip()

    # If arbiter exists, use arbiter consensus/disagreement. Else fallback to operator/skeptic votes.
    if arb:
        consensus_side = (arb.get("consensus_side") or "").strip().upper() or "NO"
        consensus_p_yes = float(arb.get("consensus_p_yes"))
        disagreement = float(arb.get("disagreement"))
        reason = "arbiter"
        p_yes = consensus_p_yes
        # Simple edge proxy: distance from 0.5 (your real edge logic can evolve later)
        edge = abs(consensus_p_yes - 0.5)
    else:
        # Fallback proxy using operator_conf vs skeptic_conf (still produces valid paper rows)
        op_side = (run.get("operator_side") or "NO").strip().upper()
        sk_side = (run.get("skeptic_side") or "NO").strip().upper()
        op_conf = float(run.get("operator_conf") or 0.5)
        sk_conf = float(run.get("skeptic_conf") or 0.5)

        # crude disagreement proxy
        disagreement = min(1.0, abs(op_conf - sk_conf))

        # crude consensus proxy
        if op_side == "YES" and sk_side == "YES":
            consensus_side = "YES"
            p_yes = max(op_conf, sk_conf)
        elif op_side == "NO" and sk_side == "NO":
            consensus_side = "NO"
            p_yes = 1.0 - max(op_conf, sk_conf)
        else:
            # split vote: lean toward higher confidence
            if op_conf >= sk_conf:
                consensus_side = op_side
                p_yes = op_conf if op_side == "YES" else (1.0 - op_conf)
            else:
                consensus_side = sk_side
                p_yes = sk_conf if sk_side == "YES" else (1.0 - sk_conf)

        consensus_p_yes = float(p_yes)
        edge = abs(consensus_p_yes - 0.5)
        reason = "runs_fallback"

    # Apply filters
    if edge < min_edge:
        return None
    if disagreement > max_disagree:
        return None

    return normalize_candidate(
        cand={
            "ts_utc": utc_now_iso(),
            "run_id": run_id or "fallback-run",
            "market_id": market_id,
            "question": question or f"Auto question for {market_id}",
            "venue": venue,
            "side": consensus_side,
            "p_yes": float(p_yes),
            "edge": float(edge),
            "consensus_p_yes": float(consensus_p_yes),
            "disagreement": float(disagreement),
            "size_usd": float(paper_size),
            "reason": reason,
            "status": "OPEN",
        },
        venue=venue,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BlackGlassLab live runner (paper trading wrapper)")
    p.add_argument("--source", default="fake", help="data source / venue tag (e.g. fake, polymarket)")
    p.add_argument("--paper", action="store_true", help="insert candidate into paper_trades")
    p.add_argument("--min-edge", type=float, default=float(os.environ.get("BGL_MIN_EDGE", "0.01")))
    p.add_argument("--max-disagree", type=float, default=float(os.environ.get("BGL_MAX_DISAGREE", "0.50")))
    p.add_argument("--paper-size", type=float, default=float(os.environ.get("BGL_PAPER_SIZE_USD", "100")))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    venue = (args.source or "fake").strip() or "fake"

    # Ensure signals dir exists even if no candidates (but only write file when we have candidates)
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    with _connect_db(DB_PATH) as conn:
        run = _latest_run(conn)

        cand: Optional[Dict[str, Any]] = None

        if args.source == "fake":
            raw = _fake_candidate(args.min_edge, args.max_disagree, args.paper_size)
            if raw:
                cand = normalize_candidate(cand=raw, venue=venue)
        else:
            # Real mode: rely on latest run + arbiter if present
            if run is not None:
                run_id = (run.get("run_id") or "").strip()
                arb = _latest_arbiter_for_run(conn, run_id) if run_id else None
                cand = build_candidate_from_db(
                    run=run,
                    arb=arb,
                    min_edge=args.min_edge,
                    max_disagree=args.max_disagree,
                    paper_size=args.paper_size,
                    venue=venue,
                )

        cands: list[Dict[str, Any]] = []
        if cand:
            cands = [cand]
            # 1) Always write candidates json when a candidate exists
            write_candidates(cands)

            # 2) Insert without NOT NULL failures (uses existing schema)
            if args.paper:
                insert_paper_trade(conn, cand)

            # Single stable summary line (no debug spam)
            print(
                "LIVE_RUNNER OK "
                f"mode={cand.get('reason')} run_id={cand.get('run_id')} market_id={cand.get('market_id')} "
                f"side={cand.get('side')} consensus_p_yes={cand.get('consensus_p_yes')} "
                f"disagreement={cand.get('disagreement')} "
                f"candidates=1 -> {CANDIDATES_PATH}"
            )
        else:
            # No candidate: do not write trade_candidates.json (your acceptance test expects it only when candidate exists)
            print("LIVE_RUNNER OK candidates=0 (no trade candidate passed filters)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
