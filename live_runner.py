#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from adapters import get_adapter

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


def normalize_candidate(*, cand: Dict[str, Any], venue: str) -> Dict[str, Any]:
    """
    Ensure candidate includes all required NOT NULL columns for paper_trades schema.
    """
    out = dict(cand)

    out["ts_utc"] = (out.get("ts_utc") or utc_now_iso())
    out["run_id"] = (out.get("run_id") or "").strip()
    out["market_id"] = (out.get("market_id") or "UNKNOWN").strip() or "UNKNOWN"

    q = (out.get("question") or "").strip()
    if not q:
        q = f"Auto question for {out['market_id']}"
    out["question"] = q

    out["venue"] = (out.get("venue") or venue).strip() or venue

    side = (out.get("side") or "").strip().upper()
    if side not in ("YES", "NO"):
        p_yes = out.get("p_yes")
        try:
            p = float(p_yes) if p_yes is not None else 0.0
        except Exception:
            p = 0.0
        side = "YES" if p >= 0.5 else "NO"
    out["side"] = side

    # Optional p_yes
    p_yes = out.get("p_yes")
    try:
        out["p_yes"] = float(p_yes) if p_yes is not None else None
    except Exception:
        out["p_yes"] = None

    # consensus_p_yes required
    cp = out.get("consensus_p_yes", out["p_yes"] if out["p_yes"] is not None else 0.5)
    try:
        out["consensus_p_yes"] = float(cp)
    except Exception:
        out["consensus_p_yes"] = 0.5

    # disagreement required
    d = out.get("disagreement", 0.0)
    try:
        out["disagreement"] = float(d)
    except Exception:
        out["disagreement"] = 0.0

    # size_usd required
    s = out.get("size_usd", 0.0)
    try:
        out["size_usd"] = float(s)
    except Exception:
        out["size_usd"] = 0.0

    # reason required
    reason = (out.get("reason") or "live_runner").strip() or "live_runner"
    out["reason"] = reason

    # status required
    status = (out.get("status") or "OPEN").strip().upper() or "OPEN"
    out["status"] = status

    # Optional numeric fields
    for k in ("edge", "brier"):
        v = out.get(k)
        if v is None:
            continue
        try:
            out[k] = float(v)
        except Exception:
            out[k] = None

    # Optional notes
    if out.get("notes") is not None:
        out["notes"] = str(out["notes"])

    return out


def paper_trade_open_exists_for_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    venue: str,
    market_id: str,
    side: str,
) -> bool:
    """
    Dedupe guard: prevent duplicate OPEN paper trades for the SAME run_id and same market/side.

    This avoids accidental double-inserts when you rerun live_runner --paper for the same run.
    It will NOT block new runs (ship_check remains deterministic).
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT 1
        FROM paper_trades
        WHERE run_id=? AND venue=? AND market_id=? AND side=? AND status='OPEN'
        LIMIT 1;
        """,
        (run_id, venue, market_id, side),
    )
    return cur.fetchone() is not None


def insert_paper_trade(conn: sqlite3.Connection, cand: Dict[str, Any]) -> None:
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
    cur.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1;")
    return _fetchone_dict(cur)


def _latest_arbiter_for_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT *
        FROM arbiter_runs
        WHERE run_id=?
        ORDER BY id DESC
        LIMIT 1;
        """,
        (run_id,),
    )
    return _fetchone_dict(cur)


def _fake_candidate(min_edge: float, max_disagree: float, paper_size: float) -> Optional[Dict[str, Any]]:
    # Deterministic fake candidate for acceptance tests
    p_yes = 0.464185
    disagree = 0.197519

    # Edge vs (stub) market baseline 0.50
    edge_vs_market = abs(p_yes - 0.5)

    if edge_vs_market < min_edge:
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
        "consensus_p_yes": round(p_yes, 6),
        "disagreement": round(disagree, 6),
        "edge": round(edge_vs_market, 6),
        "size_usd": float(paper_size),
        "reason": "fake_source",
        "status": "OPEN",
    }


def build_candidate_from_db(
    *,
    run: Dict[str, Any],
    arb: Optional[Dict[str, Any]],
    adapter_source: str,
    min_edge: float,
    max_disagree: float,
    paper_size: float,
) -> Optional[Dict[str, Any]]:
    run_id = (run.get("run_id") or "").strip() or "fallback-run"
    market_id = (run.get("market_id") or "UNKNOWN").strip() or "UNKNOWN"
    question = (run.get("question") or "").strip() or f"Auto question for {market_id}"

    # Model probability (consensus)
    if arb:
        consensus_side = (arb.get("consensus_side") or "").strip().upper() or "NO"
        consensus_p_yes = float(arb.get("consensus_p_yes"))
        disagreement = float(arb.get("disagreement"))
        reason = "arbiter"
        p_yes_model = consensus_p_yes
    else:
        op_side = (run.get("operator_side") or "NO").strip().upper()
        sk_side = (run.get("skeptic_side") or "NO").strip().upper()
        op_conf = float(run.get("operator_conf") or 0.5)
        sk_conf = float(run.get("skeptic_conf") or 0.5)
        disagreement = min(1.0, abs(op_conf - sk_conf))

        if op_side == "YES" and sk_side == "YES":
            p_yes_model = max(op_conf, sk_conf)
        elif op_side == "NO" and sk_side == "NO":
            p_yes_model = 1.0 - max(op_conf, sk_conf)
        else:
            if op_conf >= sk_conf:
                p_yes_model = op_conf if op_side == "YES" else (1.0 - op_conf)
            else:
                p_yes_model = sk_conf if sk_side == "YES" else (1.0 - sk_conf)

        consensus_p_yes = float(p_yes_model)
        consensus_side = "YES" if consensus_p_yes >= 0.5 else "NO"
        reason = "runs_fallback"

    # Market snapshot (adapter)
    adapter = get_adapter(adapter_source)
    snap = adapter.get_snapshot(market_id=market_id, question_hint=question)
    p_yes_market = float(snap.p_yes_market)

    # Trader-first edge: model vs market
    edge_vs_market = abs(float(consensus_p_yes) - p_yes_market)

    if edge_vs_market < min_edge:
        return None
    if float(disagreement) > max_disagree:
        return None

    notes_obj = {
        "p_yes_market": p_yes_market,
        "edge_vs_market": edge_vs_market,
        "adapter_venue": snap.venue,
        "adapter_extra": snap.extra,
    }

    return normalize_candidate(
        cand={
            "ts_utc": utc_now_iso(),
            "run_id": run_id,
            "market_id": market_id,
            "question": question,
            "venue": snap.venue,
            "side": consensus_side,
            "p_yes": float(p_yes_model),
            "consensus_p_yes": float(consensus_p_yes),
            "disagreement": float(disagreement),
            "edge": float(edge_vs_market),
            "size_usd": float(paper_size),
            "reason": reason,
            "status": "OPEN",
            "notes": json.dumps(notes_obj, sort_keys=True),
        },
        venue=snap.venue,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="BlackGlassLab live runner (paper trading wrapper)")
    p.add_argument("--source", default="fake", help="data source / venue tag (fake, polymarket, kalshi)")
    p.add_argument("--paper", action="store_true", help="insert candidate into paper_trades")
    p.add_argument("--min-edge", type=float, default=float(os.environ.get("BGL_MIN_EDGE", "0.01")))
    p.add_argument("--max-disagree", type=float, default=float(os.environ.get("BGL_MAX_DISAGREE", "0.50")))
    p.add_argument("--paper-size", type=float, default=float(os.environ.get("BGL_PAPER_SIZE_USD", "100")))
    return p.parse_args()


def main() -> int:
    args = parse_args()
    source = (args.source or "fake").strip().lower() or "fake"

    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)

    with _connect_db(DB_PATH) as conn:
        run = _latest_run(conn)

        cand: Optional[Dict[str, Any]] = None

        if source == "fake":
            raw = _fake_candidate(args.min_edge, args.max_disagree, args.paper_size)
            if raw:
                cand = normalize_candidate(cand=raw, venue="fake")
        else:
            if run is not None:
                run_id = (run.get("run_id") or "").strip()
                arb = _latest_arbiter_for_run(conn, run_id) if run_id else None
                cand = build_candidate_from_db(
                    run=run,
                    arb=arb,
                    adapter_source=source,
                    min_edge=args.min_edge,
                    max_disagree=args.max_disagree,
                    paper_size=args.paper_size,
                )

        if cand:
            # Always write signals when a candidate exists
            write_candidates([cand])

            inserted = False
            skipped_duplicate = False

            if args.paper:
                if paper_trade_open_exists_for_run(
                    conn,
                    run_id=cand["run_id"],
                    venue=cand["venue"],
                    market_id=cand["market_id"],
                    side=cand["side"],
                ):
                    skipped_duplicate = True
                else:
                    insert_paper_trade(conn, cand)
                    inserted = True

            suffix = ""
            if args.paper:
                suffix = " paper=inserted" if inserted else (" paper=skipped_duplicate" if skipped_duplicate else " paper=skipped")

            print(
                "LIVE_RUNNER OK "
                f"mode={cand.get('reason')} run_id={cand.get('run_id')} market_id={cand.get('market_id')} "
                f"side={cand.get('side')} consensus_p_yes={cand.get('consensus_p_yes')} "
                f"disagreement={cand.get('disagreement')} "
                f"candidates=1 -> {CANDIDATES_PATH}{suffix}"
            )
        else:
            print("LIVE_RUNNER OK candidates=0 (no trade candidate passed filters)")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
