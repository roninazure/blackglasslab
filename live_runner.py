#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from adapters import get_adapter

DB_PATH = os.path.join("memory", "runs.sqlite")
SIGNALS_DIR = Path("signals")
CANDIDATES_PATH = SIGNALS_DIR / "trade_candidates.json"
WATCHLIST_PATH = Path("markets") / "polymarket_watchlist.json"


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
    return {cols[i]: row[i] for i in range(len(cols))}


def write_candidates(cands: List[Dict[str, Any]]) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(json.dumps(cands, indent=2), encoding="utf-8")


def _safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def _compact_market_snapshot(adapter_market: Dict[str, Any]) -> Dict[str, Any]:
    """
    Take a potentially huge Polymarket market object and keep only stable, useful fields.
    This avoids giant/truncated notes blobs and makes later analysis deterministic.
    """
    keep = [
        "slug",
        "id",
        "question",
        "updatedAt",
        "endDate",
        "active",
        "closed",
        "archived",
        "volumeNum",
        "liquidityNum",
        "outcomes",
        "outcomePrices",
        "lastTradePrice",
        "bestBid",
        "bestAsk",
        "spread",
    ]
    out: Dict[str, Any] = {}
    for k in keep:
        if k in adapter_market:
            out[k] = adapter_market.get(k)
    return out


def _extract_p_yes_market(adapter_market: Dict[str, Any]) -> Optional[float]:
    """
    Prefer outcomePrices if available. For Polymarket, outcomePrices are strings in a JSON string
    or list form. We want the YES price.
    """
    op = adapter_market.get("outcomePrices")
    outcomes = adapter_market.get("outcomes")

    # Normalize outcomes/outcomePrices into Python lists if possible
    try:
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
    except Exception:
        pass

    try:
        if isinstance(op, str):
            op = json.loads(op)
    except Exception:
        pass

    if isinstance(outcomes, list) and isinstance(op, list) and len(outcomes) == len(op):
        # Find "Yes" index if present, else assume index 0 is YES
        idx = 0
        for i, name in enumerate(outcomes):
            if str(name).strip().lower() == "yes":
                idx = i
                break
        return _safe_float(op[idx])

    # Fallback to lastTradePrice if we cannot parse outcomePrices
    return _safe_float(adapter_market.get("lastTradePrice"))


def _normalize_candidate(
    *,
    cand: Dict[str, Any],
    venue: str,
    adapter_market: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Ensure candidate has all NOT NULL schema fields + a clean notes snapshot.
    Also compute market snapshot fields: p_yes_market + edge_vs_market.
    """
    out = dict(cand)

    out["venue"] = (out.get("venue") or venue or "polymarket").strip() or "polymarket"
    out["status"] = (out.get("status") or "OPEN").strip() or "OPEN"

    # Required NOT NULL
    out["question"] = (out.get("question") or "").strip() or f"Auto question for {out.get('market_id','UNKNOWN')}"
    out["reason"] = (out.get("reason") or "infer").strip() or "infer"

    # Consensus / disagreement / sizing
    out["consensus_p_yes"] = float(out.get("consensus_p_yes", out.get("p_yes", 0.5)))
    out["disagreement"] = float(out.get("disagreement", 0.0))
    out["size_usd"] = float(out.get("size_usd", 0.0))

    # Side default
    side = (out.get("side") or "").strip().upper()
    if side not in ("YES", "NO"):
        side = "YES" if out["consensus_p_yes"] >= 0.5 else "NO"
    out["side"] = side

    # Compute market snapshot
    p_yes_market = None
    snapshot: Dict[str, Any] = {}

    if adapter_market:
        snapshot = _compact_market_snapshot(adapter_market)
        p_yes_market = _extract_p_yes_market(adapter_market)

    # If we couldn't resolve market price, we still write something deterministic
    stub = False
    if p_yes_market is None:
        p_yes_market = 0.5
        stub = True

    # Signed and absolute edge
    edge_vs_market = float(out["consensus_p_yes"]) - float(p_yes_market)
    out["edge"] = float(out.get("edge", abs(edge_vs_market)))
    out["edge_vs_market"] = edge_vs_market  # not a DB column; for JSON signal convenience

    # Compact notes JSON (single source of truth for later analytics)
    notes_obj = {
        "adapter_venue": out["venue"],
        "p_yes_market": float(p_yes_market),
        "edge_vs_market": float(edge_vs_market),
        "snapshot": snapshot,
        "stub": bool(stub),
        "ts_utc": out.get("ts_utc"),
    }

    # Preserve existing notes if they exist (but keep it bounded)
    existing_notes = out.get("notes")
    if isinstance(existing_notes, str) and existing_notes.strip():
        notes_obj["prev_notes"] = existing_notes[:500]  # bounded

    out["notes"] = json.dumps(notes_obj, separators=(",", ":"), ensure_ascii=False)

    return out


def insert_paper_trade(conn: sqlite3.Connection, cand: Dict[str, Any]) -> None:
    """
    Insert must populate all NOT NULL columns for paper_trades schema.
    """
    conn.execute(
        """
        INSERT INTO paper_trades (
          run_id, ts_utc, market_id, question, venue, side,
          consensus_p_yes, disagreement, size_usd, reason, status,
          p_yes, edge, brier, notes
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?);
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
            float(cand["consensus_p_yes"]),  # keep p_yes aligned to model (not market)
            float(cand["edge"]) if cand.get("edge") is not None else None,
            float(cand["brier"]) if cand.get("brier") is not None else None,
            cand.get("notes"),
        ),
    )
    conn.commit()


def _paper_trade_exists(conn: sqlite3.Connection, run_id: str, market_id: str, venue: str) -> bool:
    cur = conn.execute(
        "SELECT 1 FROM paper_trades WHERE run_id=? AND market_id=? AND venue=? LIMIT 1;",
        (run_id, market_id, venue),
    )
    return cur.fetchone() is not None


def _load_watchlist() -> List[str]:
    if not WATCHLIST_PATH.exists():
        return []
    try:
        data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        return []
    return []


def _infer_one(
    *,
    adapter,
    slug: str,
    venue: str,
    paper_size: float,
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """
    Fetch live market data via adapter, then produce a deterministic-ish model probability.
    For Phase 1.6A, we're focused on snapshot persistence; inference remains simple/stable.
    Returns (candidate, adapter_market).
    """
    adapter_market = adapter.get_market(slug)

    question = (adapter_market.get("question") or slug).strip()
    # A stable pseudo-model probability: anchor around market price + small drift based on slug hash
    p_yes_market = _extract_p_yes_market(adapter_market)
    if p_yes_market is None:
        p_yes_market = 0.5

    drift = ((hash(slug) % 1000) / 1000.0 - 0.5) * 0.08  # +/- 4%
    model_yes = min(0.99, max(0.01, float(p_yes_market) + drift))

    disagree = abs(drift) * 3.0  # bounded-ish proxy

    side = "YES" if model_yes >= 0.5 else "NO"
    cand = {
        "ts_utc": utc_now_iso(),
        "run_id": f"infer-{utc_now_iso()}",
        "market_id": slug,
        "question": question,
        "side": side,
        "consensus_p_yes": float(model_yes),
        "disagreement": float(disagree),
        "size_usd": float(paper_size),
        "reason": "infer",
    }
    return cand, adapter_market


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--source", default="polymarket")
    ap.add_argument("--paper", action="store_true")
    ap.add_argument("--infer", action="store_true")
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=0.0)
    args = ap.parse_args()

    venue = (args.source or "polymarket").strip() or "polymarket"

    min_edge = float(os.environ.get("BGL_MIN_EDGE", "0.03"))
    max_disagree = float(os.environ.get("BGL_MAX_DISAGREE", "0.60"))
    paper_size = float(os.environ.get("BGL_PAPER_SIZE", "100"))

    adapter = get_adapter(venue)
    watchlist = _load_watchlist()

    conn = _connect_db(args.db)

    inserted = 0
    skipped_dup = 0
    loops = max(1, int(args.loops))

    for i in range(loops):
        cands: List[Dict[str, Any]] = []
        paper_status = ""

        if args.infer:
            if not watchlist:
                write_candidates([])
                print("LIVE_RUNNER OK candidates=0 (infer: empty watchlist) -> signals/trade_candidates.json")
                return 0

            slug = watchlist[i % len(watchlist)]
            raw_cand, adapter_market = _infer_one(adapter=adapter, slug=slug, venue=venue, paper_size=paper_size)

            # Normalize + snapshot
            cand = _normalize_candidate(cand=raw_cand, venue=venue, adapter_market=adapter_market)

            # Apply filters
            if float(cand["edge"]) >= min_edge and float(cand["disagreement"]) <= max_disagree:
                cands = [cand]
        else:
            # Non-infer modes are not used for Phase 1.6A. Keep signals deterministic.
            cands = []

        # Always write signals JSON (even if empty) so pipelines don't break
        write_candidates(cands)

        if cands and args.paper:
            cand = cands[0]
            if _paper_trade_exists(conn, cand["run_id"], cand["market_id"], cand["venue"]):
                skipped_dup += 1
                paper_status = "paper=skipped_duplicate"
            else:
                insert_paper_trade(conn, cand)
                inserted += 1
                paper_status = "paper=inserted"

        if cands:
            c = cands[0]
            print(
                "LIVE_RUNNER OK "
                f"mode=infer run_id={c['run_id']} market_id={c['market_id']} side={c['side']} "
                f"consensus_p_yes={c['consensus_p_yes']} disagreement={c['disagreement']} edge={c['edge']} "
                f"candidates=1 -> {CANDIDATES_PATH} {paper_status}".rstrip()
            )
        else:
            print("LIVE_RUNNER OK candidates=0 (infer no trade candidate passed filters) -> signals/trade_candidates.json")

        if args.sleep and i < loops - 1:
            time.sleep(float(args.sleep))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
