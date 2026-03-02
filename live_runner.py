#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

# Optional: adapters only used for infer mode / market snapshot
try:
    from adapters import get_adapter  # type: ignore
except Exception:
    get_adapter = None  # noqa


DB_PATH = os.path.join("memory", "runs.sqlite")
SIGNALS_DIR = Path("signals")
CANDIDATES_PATH = SIGNALS_DIR / "trade_candidates.json"
WATCHLIST_PATH = Path("markets") / "polymarket_watchlist.json"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def _kv_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    row = conn.execute("SELECT value FROM kv WHERE key=?;", (key,)).fetchone()
    return row[0] if row else None


def _kv_set(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
    conn.execute(
        "INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value;",
        (key, value),
    )


def _fetchone_dict(cur: sqlite3.Cursor) -> Optional[Dict[str, Any]]:
    row = cur.fetchone()
    if row is None:
        return None
    cols = [d[0] for d in cur.description or []]
    return {cols[i]: row[i] for i in range(len(cols))}


def _write_candidates(cands: List[Dict[str, Any]]) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    CANDIDATES_PATH.write_text(json.dumps(cands, indent=2), encoding="utf-8")


def _load_watchlist() -> List[str]:
    if not WATCHLIST_PATH.exists():
        return []
    data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    return [str(x) for x in data] if isinstance(data, list) else []


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(val)
    except Exception:
        return default


def _filters() -> Tuple[float, float, float]:
    # Support old/new env var names (avoid breakage)
    min_edge_abs = _env_float("BGL_MIN_EDGE_ABS", _env_float("BGL_MIN_EDGE", 0.03))
    min_edge_vs_market = _env_float("BGL_MIN_EDGE_VS_MARKET", 0.0)
    max_disagree = _env_float("BGL_MAX_DISAGREEMENT", _env_float("BGL_MAX_DISAGREE", 0.60))
    # NOTE: ship_check sets all of these to 0/1 so it must pass.
    return (min_edge_abs, min_edge_vs_market, max_disagree)


def _passes_filters(*, edge_abs: float, edge_vs_market: Optional[float], disagreement: float) -> bool:
    min_edge_abs, min_edge_vs_market, max_disagree = _filters()

    if disagreement > max_disagree:
        return False

    if edge_abs < min_edge_abs:
        return False

    # If we know edge_vs_market, enforce it too
    if edge_vs_market is not None and edge_vs_market < min_edge_vs_market:
        return False

    return True


def _latest_run(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1;")
    return _fetchone_dict(cur)


def _latest_arbiter_for_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM arbiter_runs WHERE run_id=? ORDER BY id DESC LIMIT 1;",
        (run_id,),
    )
    return _fetchone_dict(cur)


def _insert_paper_trade(conn: sqlite3.Connection, cand: Dict[str, Any]) -> str:
    # Skip duplicates (same run_id)
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM paper_trades WHERE run_id=? LIMIT 1;", (cand["run_id"],))
    if cur.fetchone() is not None:
        return "skipped_duplicate"

    conn.execute(
        """
        INSERT INTO paper_trades (
          run_id, ts_utc, market_id, question, venue, side,
          consensus_p_yes, disagreement, size_usd, reason, status,
          resolved_outcome, p_yes, edge, brier, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL, ?);
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
            float(cand.get("p_yes") or cand["consensus_p_yes"]),
            float(cand.get("edge") or 0.0),
            json.dumps(cand.get("notes") or {}, sort_keys=True),
        ),
    )
    conn.commit()
    return "inserted"


def _arbiter_candidate_from_db(*, conn: sqlite3.Connection, venue: str, paper_size: float) -> Optional[Dict[str, Any]]:
    run = _latest_run(conn)
    if not run:
        return None

    run_id = str(run["run_id"])
    arb = _latest_arbiter_for_run(conn, run_id)
    if not arb:
        return None

    p_yes = float(arb.get("consensus_p_yes"))
    disagreement = float(arb.get("disagreement"))
    side = "YES" if p_yes >= 0.5 else "NO"
    edge_abs = abs(p_yes - 0.5)

    cand = {
        "ts_utc": utc_now_iso(),
        "run_id": run_id,
        "market_id": str(run.get("market_id")),
        "question": str(run.get("question")),
        "venue": venue,
        "side": side,
        "p_yes": p_yes,
        "consensus_p_yes": p_yes,
        "disagreement": disagreement,
        "edge": edge_abs,
        "size_usd": float(paper_size),
        "reason": "arbiter",
        "status": "OPEN",
        "notes": {
            "mode": "arbiter",
            "edge_abs": edge_abs,
            "filters": {
                "min_edge_abs": _filters()[0],
                "min_edge_vs_market": _filters()[1],
                "max_disagree": _filters()[2],
            },
        },
    }

    # In arbiter mode, we do NOT require edge_vs_market.
    if not _passes_filters(edge_abs=edge_abs, edge_vs_market=None, disagreement=disagreement):
        return None

    return cand


def _infer_pick_slugs(conn: sqlite3.Connection, venue: str, watchlist: List[str], n_pick: int) -> List[str]:
    if not watchlist:
        return []
    # Persist cursor so we advance even when no insert happens (fixes “stuck slug”)
    key = f"infer_cursor:{venue}"
    cur_raw = _kv_get(conn, key)
    cursor = int(cur_raw) if (cur_raw and cur_raw.isdigit()) else 0

    picks: List[str] = []
    L = len(watchlist)
    for i in range(max(1, n_pick)):
        picks.append(watchlist[(cursor + i) % L])

    # Advance cursor by n_pick regardless of whether we emit/insert
    cursor = (cursor + max(1, n_pick)) % L
    _kv_set(conn, key, str(cursor))
    conn.commit()
    return picks



def _polymarket_yes_price_from_market(obj: Dict[str, Any]) -> Optional[float]:
    # Gamma returns outcomePrices like ["0.2455","0.7545"] where outcomes ["Yes","No"] commonly.
    # We assume index 0 corresponds to Yes when outcomes[0] == "Yes".
    outcomes_raw = obj.get("outcomes")
    prices_raw = obj.get("outcomePrices")
    try:
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        if not (isinstance(outcomes, list) and isinstance(prices, list) and len(outcomes) >= 2 and len(prices) >= 2):
            return None
        if str(outcomes[0]).strip().lower() == "yes":
            return float(prices[0])
        # fallback: if outcomes reversed
        if str(outcomes[1]).strip().lower() == "yes":
            return float(prices[1])
        return None
    except Exception:
        return None


def _infer_one(*, conn: sqlite3.Connection, venue: str, paper_size: float) -> Optional[Dict[str, Any]]:
    watchlist = _load_watchlist()
    if not watchlist:
        return None

    if get_adapter is None:
        return None

    # How many slugs to evaluate each loop (Phase 1.6D)
    n_pick = int(os.environ.get("BGL_INFER_BATCH", "8") or "8")
    slugs = _infer_pick_slugs(conn, venue, watchlist, n_pick)

    adapter = get_adapter(venue)

    best: Optional[Dict[str, Any]] = None
    best_q = -1.0

    for slug in slugs:
        try:
            m = adapter.get_market(slug)  # type: ignore[attr-defined]
        except Exception:
            continue

        p_yes_market = _polymarket_yes_price_from_market(m)
        if p_yes_market is None:
            p_yes_market = 0.5

        # Placeholder model until swarm inference is wired in:
        # deterministic small deviation from market to keep pipeline functional
        jitter = (hash(slug) % 2000 - 1000) / 100000.0  # [-0.01, +0.01]
        p_yes_model = min(0.99, max(0.01, p_yes_market + jitter))

        disagreement = abs(jitter) * 3.0  # 0..0.03 proxy
        edge_vs_market = p_yes_model - p_yes_market
        edge_abs = abs(p_yes_model - 0.5)

        side = "YES" if p_yes_model >= 0.5 else "NO"

        cand = {
            "ts_utc": utc_now_iso(),
            "run_id": f"infer-{utc_now_iso()}",
            "market_id": slug,
            "question": str(m.get("question") or slug),
            "venue": venue,
            "side": side,
            "p_yes": float(p_yes_model),
            "consensus_p_yes": float(p_yes_model),
            "disagreement": float(disagreement),
            # edge in output = abs(edge_vs_market) when we have market odds
            "edge": float(abs(edge_vs_market)),
            "size_usd": float(paper_size),
            "reason": "infer",
            "status": "OPEN",
            "notes": {
                "adapter_venue": venue,
                "p_yes_market": float(p_yes_market),
                "edge_vs_market": float(edge_vs_market),
                "snapshot": {
                    "slug": slug,
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "updatedAt": m.get("updatedAt"),
                },
            },
        }

        # Filter gate
        if not _passes_filters(edge_abs=edge_abs, edge_vs_market=edge_vs_market, disagreement=disagreement):
            continue

        # Rank: prioritize edge vs market, penalize disagreement
        q = abs(edge_vs_market) * (1.0 - 0.75 * disagreement)
        if q > best_q:
            best_q = q
            best = cand

    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--source", default="polymarket")
    ap.add_argument("--paper", action="store_true")
    ap.add_argument("--infer", action="store_true")
    ap.add_argument("--mode", choices=["arbiter", "infer"], default=None)
    ap.add_argument("--loops", type=int, default=1)
    ap.add_argument("--sleep", type=float, default=1.0)
    args = ap.parse_args()

    venue = str(args.source).strip().lower()
    paper_size = float(os.environ.get("BGL_PAPER_SIZE", "100") or "100")

    # Determine mode explicitly
    mode = args.mode or ("infer" if args.infer else "arbiter")

    conn = _connect_db(args.db)

    emitted = 0
    last_print = ""

    for i in range(int(args.loops)):
        cand: Optional[Dict[str, Any]] = None

        if mode == "arbiter":
            cand = _arbiter_candidate_from_db(conn=conn, venue=venue, paper_size=paper_size)
        else:
            cand = _infer_one(conn=conn, venue=venue, paper_size=paper_size)

        cands: List[Dict[str, Any]] = []
        if cand is not None:
            cands = [cand]
            emitted = 1

        _write_candidates(cands)

        paper_status = ""
        if args.paper and cand is not None:
            paper_status = "paper=" + _insert_paper_trade(conn, cand)

        if cand is None:
            # Print clear mode-specific message
            msg = f"LIVE_RUNNER OK candidates=0 ({mode} no trade candidate passed filters) -> {CANDIDATES_PATH}"
            print(msg)
            last_print = msg
        else:
            msg = (
                f"LIVE_RUNNER OK mode={mode} run_id={cand['run_id']} market_id={cand['market_id']} "
                f"side={cand['side']} consensus_p_yes={cand['consensus_p_yes']} disagreement={cand['disagreement']} "
                f"edge={cand.get('edge')} candidates=1 -> {CANDIDATES_PATH} {paper_status}".rstrip()
            )
            print(msg)
            last_print = msg

        if i < int(args.loops) - 1:
            time.sleep(float(args.sleep))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
