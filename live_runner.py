#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, List, Tuple

try:
    from adapters import get_adapter  # type: ignore
except Exception:
    get_adapter = None  # noqa

try:
    from llm.openai_client import openai_enabled, forecast_yes_probability
except Exception:
    openai_enabled = lambda: False  # type: ignore
    forecast_yes_probability = None  # type: ignore

from models.baseline import score_market, market_yes_price

DB_PATH = os.path.join("memory", "runs.sqlite")
SIGNALS_DIR = Path("signals")
WATCHLIST_PATH = Path("markets") / "polymarket_watchlist.json"


def _candidates_path(mode: str) -> Path:
    if mode == "arbiter":
        return SIGNALS_DIR / "trade_candidates_arbiter.json"
    if mode == "infer":
        return SIGNALS_DIR / "trade_candidates_infer.json"
    return SIGNALS_DIR / "trade_candidates.json"


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


def _write_candidates(mode: str, cands: List[Dict[str, Any]]) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    _candidates_path(mode).write_text(json.dumps(cands, indent=2), encoding="utf-8")


def _write_infer_diagnostics(payload: dict) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    (SIGNALS_DIR / "infer_diagnostics.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _load_watchlist() -> List[str]:
    if not WATCHLIST_PATH.exists():
        return []

    data = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        return []

    slugs: List[str] = []
    for x in data:
        if isinstance(x, str):
            t = x.strip()
            if t:
                slugs.append(t)
            continue
        if isinstance(x, dict):
            cand = x.get("market_id") or x.get("slug") or x.get("id")
            if isinstance(cand, str) and cand.strip():
                slugs.append(cand.strip())

    out: List[str] = []
    seen = set()
    for slug in slugs:
        if slug not in seen:
            out.append(slug)
            seen.add(slug)
    return out


def _env_float(name: str, default: float) -> float:
    val = os.environ.get(name)
    if val is None or str(val).strip() == "":
        return default
    try:
        return float(val)
    except Exception:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    if v is None:
        return default
    t = str(v).strip().lower()
    if t in ("1", "true", "yes", "y", "on"):
        return True
    if t in ("0", "false", "no", "n", "off", ""):
        return False
    return default


def _filters() -> Tuple[float, float, float]:
    min_edge_abs = _env_float("BGL_MIN_EDGE_ABS", _env_float("BGL_MIN_EDGE", 0.03))
    min_edge_vs_market = _env_float("BGL_MIN_EDGE_VS_MARKET", 0.0)
    max_disagree = _env_float("BGL_MAX_DISAGREEMENT", _env_float("BGL_MAX_DISAGREE", 0.60))
    return (min_edge_abs, min_edge_vs_market, max_disagree)


def _infer_rejection_reason(*, edge_abs: float, edge_vs_market: float, disagreement: float) -> str:
    min_edge_abs, min_edge_vs_market, max_disagree = _filters()
    if disagreement > max_disagree:
        return "max_disagree"
    if edge_abs < min_edge_abs:
        return "min_edge_abs"
    if abs(edge_vs_market) < min_edge_vs_market:
        return "min_edge_vs_market"
    return "pass"


def _passes_filters(*, edge_abs: float, edge_vs_market: Optional[float], disagreement: float) -> bool:
    return _infer_rejection_reason(
        edge_abs=edge_abs,
        edge_vs_market=float(edge_vs_market or 0.0),
        disagreement=disagreement,
    ) == "pass"


def _latest_run(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1;")
    return _fetchone_dict(cur)


def _latest_arbiter_for_run(conn: sqlite3.Connection, run_id: str) -> Optional[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute("SELECT * FROM arbiter_runs WHERE run_id=? ORDER BY id DESC LIMIT 1;", (run_id,))
    return _fetchone_dict(cur)


def _insert_paper_trade(conn: sqlite3.Connection, cand: Dict[str, Any]) -> str:
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

    if not _passes_filters(edge_abs=edge_abs, edge_vs_market=None, disagreement=disagreement):
        return None

    return cand


def _infer_recent_slugs(conn: sqlite3.Connection, venue: str, n: int) -> list[str]:
    if n <= 0:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT market_id
        FROM paper_trades
        WHERE venue=? AND reason='infer'
        ORDER BY id DESC
        LIMIT ?;
        """,
        (venue, n),
    )
    return [r[0] for r in cur.fetchall() if r and r[0]]


def _infer_pick_slugs_batch(conn: sqlite3.Connection, watchlist: list[str], batch: int) -> tuple[list[str], int]:
    if not watchlist:
        return ([], 0)

    n = len(watchlist)
    cur_raw = _kv_get(conn, "infer_cursor") or "0"
    try:
        cursor = int(cur_raw)
    except Exception:
        cursor = 0

    batch = max(1, int(batch))
    take = min(batch, n)
    slugs = [watchlist[(cursor + i) % n] for i in range(take)]
    next_cursor = (cursor + take) % n
    return (slugs, next_cursor)


def _infer_one(*, conn: sqlite3.Connection, venue: str, paper_size: float) -> Optional[Dict[str, Any]]:
    watchlist = _load_watchlist()
    if not watchlist or get_adapter is None:
        return None

    batch = int(os.environ.get("BGL_INFER_BATCH", "8") or "8")
    cooldown_n = int(os.environ.get("BGL_INFER_COOLDOWN", "0") or "0")

    slugs, next_cursor = _infer_pick_slugs_batch(conn, watchlist, batch)
    _kv_set(conn, "infer_cursor", str(next_cursor))
    conn.commit()

    recent = set(_infer_recent_slugs(conn, venue, cooldown_n))
    adapter = get_adapter(venue)

    infer_diag_rows: list[dict] = []
    infer_diag_counts = {
        "evaluated": 0,
        "passed": 0,
        "rejected": {
            "fetch_failed": 0,
            "inactive_market": 0,
            "closed_market": 0,
            "low_liquidity": 0,
            "low_volume": 0,
            "wide_spread": 0,
            "max_disagree": 0,
            "min_edge_abs": 0,
            "min_edge_vs_market": 0,
        },
    }

    for slug in slugs:
        if cooldown_n > 0 and slug in recent:
            continue

        try:
            m = adapter.get_market(slug)  # type: ignore[attr-defined]
        except Exception as e:
            infer_diag_counts["evaluated"] += 1
            infer_diag_counts["rejected"]["fetch_failed"] += 1
            infer_diag_rows.append({
                "slug": slug,
                "decision": "REJECT",
                "reason": "fetch_failed",
                "error": str(e)[:500],
            })
            continue

        p_yes_market, spread, pricing_source = market_yes_price(m)

        use_llm = (
            _env_bool("BGL_INFER_USE_LLM", False)
            and openai_enabled()
            and (forecast_yes_probability is not None)
        )

        llm_rationale = ""
        llm_conf = 0.0
        baseline = score_market(m)

        if baseline.reject_reason is not None:
            infer_diag_counts["evaluated"] += 1
            infer_diag_counts["rejected"][baseline.reject_reason] += 1
            infer_diag_rows.append({
                "slug": slug,
                "question": str(m.get("question") or slug),
                "p_yes_market": float(baseline.p_yes_market),
                "p_yes_model": float(baseline.p_yes_model),
                "edge_vs_market": float(baseline.p_yes_model - baseline.p_yes_market),
                "edge_abs": float(abs(baseline.p_yes_model - 0.5)),
                "disagreement": 1.0,
                "side": "YES" if baseline.p_yes_model >= 0.5 else "NO",
                "decision": "REJECT",
                "reason": baseline.reject_reason,
                "pricing_source": pricing_source,
                "spread": float(spread),
                "components": baseline.components,
            })
            continue

        if use_llm:
            ctx = {
                "venue": venue,
                "slug": slug,
                "p_yes_market": float(p_yes_market),
                "market_snapshot": {
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "updatedAt": m.get("updatedAt"),
                    "outcomes": m.get("outcomes"),
                    "outcomePrices": m.get("outcomePrices"),
                    "bestBid": m.get("bestBid"),
                    "bestAsk": m.get("bestAsk"),
                    "lastTradePrice": m.get("lastTradePrice"),
                    "volume": m.get("volume"),
                    "liquidity": m.get("liquidity"),
                },
                "policy": {"return_json_only": True, "paper_only": True},
            }
            p_yes_model, llm_conf, llm_rationale = forecast_yes_probability(
                question=str(m.get("question") or slug),
                context=ctx,
            )
            p_yes_model = float(min(0.99, max(0.01, p_yes_model)))
            disagreement = float(max(0.0, min(1.0, 1.0 - float(llm_conf))))
            components = {}
        else:
            p_yes_model = float(baseline.p_yes_model)
            llm_conf = float(baseline.confidence)
            disagreement = float(max(0.0, min(1.0, 1.0 - baseline.confidence)))
            components = baseline.components

        edge_vs_market = float(p_yes_model - p_yes_market)
        edge_abs = abs(p_yes_model - 0.5)
        side = "YES" if p_yes_model >= 0.5 else "NO"

        infer_diag_counts["evaluated"] += 1
        reason = _infer_rejection_reason(edge_abs=edge_abs, edge_vs_market=edge_vs_market, disagreement=disagreement)
        infer_diag_rows.append({
            "slug": slug,
            "question": str(m.get("question") or slug),
            "p_yes_market": float(p_yes_market),
            "p_yes_model": float(p_yes_model),
            "edge_vs_market": float(edge_vs_market),
            "edge_abs": float(edge_abs),
            "disagreement": float(disagreement),
            "side": side,
            "decision": "PASS" if reason == "pass" else "REJECT",
            "reason": reason,
            "pricing_source": pricing_source,
            "spread": float(spread),
            "components": components,
        })
        if reason != "pass":
            infer_diag_counts["rejected"][reason] += 1

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
            "edge": float(abs(edge_vs_market)),
            "size_usd": float(paper_size),
            "reason": "infer",
            "status": "OPEN",
            "notes": {
                "adapter_venue": venue,
                "p_yes_market": float(p_yes_market),
                "edge_vs_market": float(edge_vs_market),
                "pricing_source": pricing_source,
                "spread": float(spread),
                "llm": {
                    "enabled": bool(use_llm),
                    "model": os.environ.get("BGL_LLM_MODEL", ""),
                    "confidence": float(llm_conf),
                    "rationale": llm_rationale,
                },
                "baseline_components": components,
                "snapshot": {
                    "slug": slug,
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "updatedAt": m.get("updatedAt"),
                    "volume": m.get("volume"),
                    "liquidity": m.get("liquidity"),
                    "bestBid": m.get("bestBid"),
                    "bestAsk": m.get("bestAsk"),
                    "lastTradePrice": m.get("lastTradePrice"),
                },
            },
        }

        if _passes_filters(edge_abs=edge_abs, edge_vs_market=edge_vs_market, disagreement=disagreement):
            infer_diag_counts["passed"] += 1
            _write_infer_diagnostics({
                "ts_utc": utc_now_iso(),
                "source": venue,
                "mode": "infer",
                "settings": {
                    "batch": int(os.environ.get("BGL_INFER_BATCH", "8") or "8"),
                    "cooldown": int(os.environ.get("BGL_INFER_COOLDOWN", "0") or "0"),
                    "min_edge_abs": _filters()[0],
                    "min_edge_vs_market": _filters()[1],
                    "max_disagree": _filters()[2],
                    "paper_size": float(os.environ.get("BGL_PAPER_SIZE", "100") or "100"),
                },
                "summary": infer_diag_counts,
                "rows": infer_diag_rows,
            })
            return cand

    _write_infer_diagnostics({
        "ts_utc": utc_now_iso(),
        "source": venue,
        "mode": "infer",
        "settings": {
            "batch": int(os.environ.get("BGL_INFER_BATCH", "8") or "8"),
            "cooldown": int(os.environ.get("BGL_INFER_COOLDOWN", "0") or "0"),
            "min_edge_abs": _filters()[0],
            "min_edge_vs_market": _filters()[1],
            "max_disagree": _filters()[2],
            "paper_size": float(os.environ.get("BGL_PAPER_SIZE", "100") or "100"),
        },
        "summary": infer_diag_counts,
        "rows": infer_diag_rows,
    })
    return None


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
    mode = args.mode or ("infer" if args.infer else "arbiter")

    conn = _connect_db(args.db)

    for i in range(int(args.loops)):
        cand: Optional[Dict[str, Any]] = None

        if mode == "arbiter":
            cand = _arbiter_candidate_from_db(conn=conn, venue=venue, paper_size=paper_size)
        else:
            cand = _infer_one(conn=conn, venue=venue, paper_size=paper_size)

        cands: List[Dict[str, Any]] = [cand] if cand is not None else []
        _write_candidates(mode, cands)

        paper_status = ""
        if args.paper and cand is not None:
            paper_status = "paper=" + _insert_paper_trade(conn, cand)

        if cand is None:
            print(f"LIVE_RUNNER OK candidates=0 ({mode} no trade candidate passed filters) -> {_candidates_path(mode)}")
        else:
            print(
                f"LIVE_RUNNER OK mode={mode} run_id={cand['run_id']} market_id={cand['market_id']} "
                f"side={cand['side']} consensus_p_yes={cand['consensus_p_yes']} disagreement={cand['disagreement']} "
                f"edge={cand.get('edge')} candidates=1 -> {_candidates_path(mode)} {paper_status}".rstrip()
            )

        if i < int(args.loops) - 1:
            time.sleep(float(args.sleep))

    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
