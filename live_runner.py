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

from context.temporal import build_temporal_context, validate_temporal_rationale
from models.baseline import score_market, market_yes_price

DB_PATH = os.path.join("memory", "runs.sqlite")
SIGNALS_DIR = Path("signals")
WATCHLIST_PATH = Path("markets") / "polymarket_watchlist.json"
PIPELINE_REPORT_PATH = SIGNALS_DIR / "infer_pipeline_report.json"

PIPELINE_SUMMARY_FIELDS = (
    "watchlist_total",
    "blocked_existing_position",
    "skipped_category_cap",
    "fetch_attempted",
    "fetch_failed",
    "inactive_or_closed",
    "invalid_price",
    "extreme_tail",
    "liquidity_rejected",
    "volume_rejected",
    "spread_rejected",
    "time_rejected",
    "llm_attempted",
    "llm_failed",
    "edge_rejected",
    "disagreement_rejected",
    "temporal_inconsistency",
    "diagnostics_written",
    "candidates_generated",
    "paper_inserted",
    "paper_pending",
    "paper_duplicate",
    "paper_not_requested",
)


def _candidates_path(mode: str) -> Path:
    if mode == "arbiter":
        return SIGNALS_DIR / "trade_candidates_arbiter.json"
    if mode == "infer":
        return SIGNALS_DIR / "trade_candidates_infer.json"
    return SIGNALS_DIR / "trade_candidates.json"


def utc_now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


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


def _write_pipeline_report(payload: dict) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    PIPELINE_REPORT_PATH.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def _new_pipeline_report(watchlist: List[str], venue: str) -> Dict[str, Any]:
    ts = utc_now_iso()
    summary = {field: 0 for field in PIPELINE_SUMMARY_FIELDS}
    summary["watchlist_total"] = len(watchlist)
    return {
        "run_id": f"infer-{ts}",
        "ts_utc": ts,
        "source": venue,
        "summary": summary,
        "markets": [
            {
                "market_id": slug,
                "final_stage": "watchlist_loaded",
                "decision": "SKIP",
                "reason": "unclassified",
                "details": {},
            }
            for slug in watchlist
        ],
    }


def _finalize_pipeline_market(
    record: Dict[str, Any],
    *,
    final_stage: str,
    decision: str,
    reason: str,
    details: Optional[Dict[str, Any]] = None,
) -> None:
    record["final_stage"] = final_stage
    record["decision"] = decision
    record["reason"] = reason
    record["details"] = details or {}


def _existing_position_slugs(conn: sqlite3.Connection, venue: str) -> set[str]:
    rows = conn.execute(
        "SELECT market_id FROM paper_trades WHERE venue=? AND status IN ('OPEN','PENDING')",
        (venue,),
    ).fetchall()
    return {str(row[0]) for row in rows if row and row[0]}


def _print_pipeline_funnel(report: Dict[str, Any]) -> None:
    s = report["summary"]
    rejected_total = (
        s["inactive_or_closed"]
        + s["invalid_price"]
        + s["extreme_tail"]
        + s["fetch_failed"]
        + s["liquidity_rejected"]
        + s["volume_rejected"]
        + s["spread_rejected"]
        + s["time_rejected"]
        + s["edge_rejected"]
        + s["disagreement_rejected"]
        + s.get("temporal_inconsistency", 0)
    )
    print(
        "PIPELINE "
        f"watchlist={s['watchlist_total']} fetched={s['fetch_attempted'] - s['fetch_failed']} "
        f"llm={s['llm_attempted']} rejected={rejected_total} "
        f"candidates={s['candidates_generated']}",
        flush=True,
    )
    print(
        "SKIPS "
        f"existing={s['blocked_existing_position']} category={s['skipped_category_cap']} "
        f"fetch_failed={s['fetch_failed']} inactive={s['inactive_or_closed']} "
        f"tail={s['extreme_tail']} edge={s['edge_rejected']} temporal={s.get('temporal_inconsistency', 0)}",
        flush=True,
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
    # Block if any open OR pending position exists for this market
    cur.execute(
        "SELECT 1 FROM paper_trades WHERE market_id=? AND venue=? AND status IN ('OPEN','PENDING') LIMIT 1;",
        (cand["market_id"], cand["venue"]),
    )
    if cur.fetchone() is not None:
        return "skipped_duplicate"

    # Use PENDING status when approval gate is enabled
    require_approval = os.environ.get("BGL_REQUIRE_APPROVAL", "1").strip() in ("1", "true", "yes")
    status = "PENDING" if require_approval else "OPEN"

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
            status,
            float(cand.get("p_yes") or cand["consensus_p_yes"]),
            float(cand.get("edge") or 0.0),
            json.dumps(cand.get("notes") or {}, sort_keys=True),
        ),
    )
    conn.commit()
    return f"queued_for_approval" if status == "PENDING" else "inserted"


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
    market_id = str(run.get("market_id"))

    if get_adapter is None:
        print(
            f"[WARN] arbiter candidate skipped for market_id={market_id}: adapter registry unavailable",
            flush=True,
        )
        return None

    try:
        adapter = get_adapter(venue)
        m = adapter.get_market(market_id)  # type: ignore[attr-defined]
        p_mkt, spread, pricing_source = market_yes_price(m)
    except Exception as e:
        print(
            f"[WARN] arbiter candidate skipped for market_id={market_id}: market snapshot fetch failed: {str(e)[:500]}",
            flush=True,
        )
        return None

    if pricing_source == "fallback":
        print(
            f"[WARN] arbiter candidate skipped for market_id={market_id}: market price unavailable (pricing_source=fallback)",
            flush=True,
        )
        return None

    p_yes_market = float(p_mkt)
    edge_vs_market = float(p_yes - p_yes_market)
    edge_abs = abs(edge_vs_market)
    side = "YES" if edge_vs_market > 0 else "NO"

    cand = {
        "ts_utc": utc_now_iso(),
        "run_id": run_id,
        "market_id": market_id,
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
            "p_yes_market": float(p_yes_market),
            "edge_vs_market": float(edge_vs_market),
            "edge_abs": float(edge_abs),
            "pricing_source": pricing_source,
            "spread": float(spread),
            "filters": {
                "min_edge_abs": _filters()[0],
                "min_edge_vs_market": _filters()[1],
                "max_disagree": _filters()[2],
            },
        },
    }

    if not _passes_filters(edge_abs=edge_abs, edge_vs_market=edge_vs_market, disagreement=disagreement):
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


def _topic_label(question: str) -> str:
    """Classify market question into a broad category for concentration tracking."""
    q = question.lower()
    checks = [
        (["election", "ballot", "referendum", "primary", "vote"],      "politics"),
        (["president", "congress", "senate", "parliament", "prime minister"], "politics"),
        (["trump", "harris", "biden", "executive order", "impeach"],   "politics"),
        (["federal reserve", "rate cut", "rate hike", "fomc",
          "interest rate", "fed funds"],                                "macro/fed"),
        (["recession", "inflation", "gdp", "tariff", "cpi",
          "unemployment"],                                              "macro/econ"),
        (["ceasefire", "invasion", "nuclear", "conflict",
          "sanction", "coup"],                                          "geopolitics"),
        (["supreme court", "scotus", "verdict", "indictment",
          "lawsuit"],                                                   "legal"),
        (["bitcoin", "btc", "ethereum", "eth", "crypto",
          "solana"],                                                    "crypto"),
        (["ipo", "acquisition", "merger", "bankruptcy"],               "corporate"),
    ]
    for keywords, label in checks:
        if any(kw in q for kw in keywords):
            return label
    return "other"


def _category_cap_ok(conn: sqlite3.Connection, category: str) -> bool:
    """Return True if opening another position in this category is within the cap."""
    max_per = int(os.environ.get("BGL_MAX_PER_CATEGORY", "3") or "3")
    # Parse notes JSON in Python — LIKE on JSON text is order-sensitive and fragile
    rows = conn.execute(
        "SELECT notes FROM paper_trades WHERE status IN ('OPEN', 'PENDING')"
    ).fetchall()
    count = 0
    for (notes_str,) in rows:
        try:
            notes = json.loads(notes_str or "{}")
            if notes.get("category") == category:
                count += 1
        except Exception:
            pass
    return count < max_per


def _infer_one(
    *, conn: sqlite3.Connection, venue: str, paper_size: float
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    watchlist = _load_watchlist()
    report = _new_pipeline_report(watchlist, venue)
    records = {row["market_id"]: row for row in report["markets"]}
    summary = report["summary"]
    if not watchlist or get_adapter is None:
        reason = "empty_watchlist" if not watchlist else "adapter_unavailable"
        for record in report["markets"]:
            _finalize_pipeline_market(
                record,
                final_stage="setup",
                decision="SKIP",
                reason=reason,
            )
        summary["finalized_markets"] = len(report["markets"])
        return (None, report)

    batch = int(os.environ.get("BGL_INFER_BATCH", "8") or "8")
    cooldown_n = int(os.environ.get("BGL_INFER_COOLDOWN", "0") or "0")

    slugs, next_cursor = _infer_pick_slugs_batch(conn, watchlist, batch)
    _kv_set(conn, "infer_cursor", str(next_cursor))
    conn.commit()

    recent = set(_infer_recent_slugs(conn, venue, cooldown_n))
    existing = _existing_position_slugs(conn, venue)
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
            "time_rejected": 0,
            "invalid_price": 0,
            "extreme_tail": 0,
            "category_cap": 0,
            "max_disagree": 0,
            "min_edge_abs": 0,
            "min_edge_vs_market": 0,
            "temporal_inconsistency": 0,
        },
    }

    selected = set(slugs)
    for slug in watchlist:
        record = records[slug]
        if slug in existing:
            summary["blocked_existing_position"] += 1
            _finalize_pipeline_market(
                record,
                final_stage="existing_position_filter",
                decision="SKIP",
                reason="existing_open_or_pending_position",
            )
        elif slug not in selected:
            _finalize_pipeline_market(
                record,
                final_stage="batch_selection",
                decision="SKIP",
                reason="not_selected_in_batch",
                details={"batch": batch},
            )

    candidate: Optional[Dict[str, Any]] = None

    for slug in slugs:
        record = records[slug]
        if slug in existing:
            continue
        if cooldown_n > 0 and slug in recent:
            _finalize_pipeline_market(
                record,
                final_stage="cooldown_filter",
                decision="SKIP",
                reason="recent_infer_cooldown",
                details={"cooldown": cooldown_n},
            )
            continue

        if candidate is not None:
            _finalize_pipeline_market(
                record,
                final_stage="candidate_limit",
                decision="SKIP",
                reason="candidate_limit_reached",
                details={"max_candidates_per_run": 1},
            )
            continue

        summary["fetch_attempted"] += 1
        try:
            m = adapter.get_market(slug)  # type: ignore[attr-defined]
        except Exception as e:
            summary["fetch_failed"] += 1
            infer_diag_counts["evaluated"] += 1
            infer_diag_counts["rejected"]["fetch_failed"] += 1
            infer_diag_rows.append({
                "slug": slug,
                "decision": "REJECT",
                "reason": "fetch_failed",
                "error": str(e)[:500],
            })
            _finalize_pipeline_market(
                record,
                final_stage="api_lookup",
                decision="REJECT",
                reason="fetch_failed",
                details={"error": str(e)[:500]},
            )
            continue

        temporal_context = build_temporal_context(
            m,
            question=str(m.get("question") or slug),
            slug=slug,
        )
        if temporal_context.get("requires_verified_temporal_context") and temporal_context.get("event_status") == "UNKNOWN":
            summary["temporal_inconsistency"] += 1
            infer_diag_counts["evaluated"] += 1
            infer_diag_counts["rejected"]["temporal_inconsistency"] += 1
            infer_diag_rows.append({
                "slug": slug,
                "question": str(m.get("question") or slug),
                "decision": "REJECT",
                "reason": "temporal_inconsistency",
                "temporal_context": temporal_context,
                "temporal_error": "missing_verified_temporal_context",
            })
            _finalize_pipeline_market(
                record,
                final_stage="temporal_validation",
                decision="REJECT",
                reason="temporal_inconsistency",
                details={
                    "temporal_error": "missing_verified_temporal_context",
                    "temporal_context": temporal_context,
                },
            )
            continue

        p_yes_market, spread, pricing_source = market_yes_price(m)

        if pricing_source == "fallback":
            summary["invalid_price"] += 1
            infer_diag_counts["evaluated"] += 1
            infer_diag_counts["rejected"]["invalid_price"] += 1
            infer_diag_rows.append({
                "slug": slug,
                "question": str(m.get("question") or slug),
                "decision": "REJECT",
                "reason": "invalid_price",
                "pricing_source": pricing_source,
            })
            _finalize_pipeline_market(
                record,
                final_stage="price_validation",
                decision="REJECT",
                reason="invalid_price",
                details={"pricing_source": pricing_source},
            )
            continue

        # Skip extreme tail markets — crowd < 3% or > 97% are too illiquid/noisy
        min_crowd = _env_float("BGL_MIN_CROWD_PRICE", 0.03)
        max_crowd = 1.0 - min_crowd
        if p_yes_market < min_crowd or p_yes_market > max_crowd:
            summary["extreme_tail"] += 1
            infer_diag_counts["evaluated"] += 1
            infer_diag_counts["rejected"]["extreme_tail"] += 1
            infer_diag_rows.append({
                "slug": slug, "decision": "REJECT",
                "reason": "extreme_tail",
                "p_yes_market": float(p_yes_market),
            })
            _finalize_pipeline_market(
                record,
                final_stage="tail_filter",
                decision="REJECT",
                reason="extreme_tail",
                details={"p_yes_market": float(p_yes_market)},
            )
            continue

        use_llm = (
            _env_bool("BGL_INFER_USE_LLM", False)
            and openai_enabled()
            and (forecast_yes_probability is not None)
        )

        llm_rationale = ""
        llm_conf = 0.0
        baseline = score_market(m)

        if baseline.reject_reason is not None:
            summary_key = {
                "inactive_market": "inactive_or_closed",
                "closed_market": "inactive_or_closed",
                "low_liquidity": "liquidity_rejected",
                "low_volume": "volume_rejected",
                "wide_spread": "spread_rejected",
                "time_rejected": "time_rejected",
            }.get(baseline.reject_reason)
            if summary_key:
                summary[summary_key] += 1
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
            _finalize_pipeline_market(
                record,
                final_stage="market_quality_filter",
                decision="REJECT",
                reason=baseline.reject_reason,
                details={
                    "p_yes_market": float(baseline.p_yes_market),
                    "spread": float(spread),
                    "pricing_source": pricing_source,
                },
            )
            continue

        llm_used = False
        llm_error = ""
        if use_llm:
            summary["llm_attempted"] += 1
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
            try:
                p_yes_model, llm_conf, llm_rationale = forecast_yes_probability(
                    question=str(m.get("question") or slug),
                    context=ctx,
                )
                p_yes_model = float(min(0.99, max(0.01, p_yes_model)))
                disagreement = float(max(0.0, min(1.0, 1.0 - float(llm_conf))))
                components = {}
                llm_used = True
                valid_temporal, temporal_reason, temporal_details = validate_temporal_rationale(
                    llm_rationale,
                    temporal_context,
                    question=str(m.get("question") or slug),
                )
                if not valid_temporal:
                    summary["temporal_inconsistency"] += 1
                    infer_diag_counts["evaluated"] += 1
                    infer_diag_counts["rejected"]["temporal_inconsistency"] += 1
                    infer_diag_rows.append({
                        "slug": slug,
                        "question": str(m.get("question") or slug),
                        "p_yes_market": float(p_yes_market),
                        "p_yes_model": float(p_yes_model),
                        "edge_vs_market": float(p_yes_model - p_yes_market),
                        "edge_abs": float(abs(p_yes_model - p_yes_market)),
                        "disagreement": float(disagreement),
                        "llm_confidence": float(llm_conf),
                        "llm_rationale": llm_rationale,
                        "llm_used": llm_used,
                        "side": "YES" if p_yes_model >= 0.5 else "NO",
                        "decision": "REJECT",
                        "reason": "temporal_inconsistency",
                        "pricing_source": pricing_source,
                        "spread": float(spread),
                        "components": components,
                        "temporal_validation": temporal_reason,
                        "temporal_details": temporal_details,
                    })
                    _finalize_pipeline_market(
                        record,
                        final_stage="temporal_validation",
                        decision="REJECT",
                        reason="temporal_inconsistency",
                        details={
                            "temporal_validation": temporal_reason,
                            "temporal_details": temporal_details,
                            "llm_used": llm_used,
                        },
                    )
                    continue
            except Exception as llm_err:
                err_msg = str(llm_err)
                llm_error = err_msg[:500]
                summary["llm_failed"] += 1
                print(f"[WARN] LLM call failed, falling back to baseline: {err_msg[:120]}", flush=True)
                if "billing" in err_msg.lower() or "credit" in err_msg.lower():
                    # Disable LLM for rest of this run to avoid spamming API errors
                    use_llm = False
                p_yes_model = float(baseline.p_yes_model)
                llm_conf = float(baseline.confidence)
                disagreement = float(max(0.0, min(1.0, 1.0 - baseline.confidence)))
                components = baseline.components
        else:
            p_yes_model = float(baseline.p_yes_model)
            llm_conf = float(baseline.confidence)
            disagreement = float(max(0.0, min(1.0, 1.0 - baseline.confidence)))
            components = baseline.components

        edge_vs_market = float(p_yes_model - p_yes_market)
        # Fix: use edge vs market price, not vs 0.5
        # abs(model - 0.5) was always huge (e.g. 0.46) so BGL_MIN_EDGE_ABS never blocked anything
        edge_abs = abs(edge_vs_market)
        side = "YES" if edge_vs_market > 0 else "NO"

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
            "llm_confidence": float(llm_conf),
            "llm_rationale": llm_rationale,
            "llm_used": llm_used,
            "side": side,
            "decision": "PASS" if reason == "pass" else "REJECT",
            "reason": reason,
            "pricing_source": pricing_source,
            "spread": float(spread),
            "components": components,
            "temporal_context": temporal_context,
        })
        if reason != "pass":
            infer_diag_counts["rejected"][reason] += 1
            if reason == "max_disagree":
                summary["disagreement_rejected"] += 1
            else:
                summary["edge_rejected"] += 1
            details = {
                "p_yes_market": float(p_yes_market),
                "p_yes_model": float(p_yes_model),
                "edge_vs_market": float(edge_vs_market),
                "disagreement": float(disagreement),
                "llm_used": llm_used,
            }
            if llm_error:
                details["llm_error"] = llm_error
                details["fallback"] = "baseline"
            _finalize_pipeline_market(
                record,
                final_stage="disagreement_filter" if reason == "max_disagree" else "edge_filter",
                decision="REJECT",
                reason=reason,
                details=details,
            )
            continue

        cand = {
            "ts_utc": utc_now_iso(),
            "run_id": report["run_id"],
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
                "category": _topic_label(str(m.get("question") or slug)),
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

        category = _topic_label(str(m.get("question") or slug))
        if not _category_cap_ok(conn, category):
            summary["skipped_category_cap"] += 1
            infer_diag_counts["rejected"]["category_cap"] += 1
            infer_diag_rows[-1]["decision"] = "REJECT"
            infer_diag_rows[-1]["reason"] = "category_cap"
            print(f"  [infer] category cap reached for '{category}' - skipping {slug}", flush=True)
            _finalize_pipeline_market(
                record,
                final_stage="category_cap",
                decision="SKIP",
                reason="category_cap_reached",
                details={"category": category},
            )
            continue
        infer_diag_counts["passed"] += 1
        summary["candidates_generated"] += 1
        candidate = cand
        details = {
            "category": category,
            "side": side,
            "edge_vs_market": float(edge_vs_market),
            "disagreement": float(disagreement),
            "llm_used": llm_used,
        }
        if llm_error:
            details["llm_error"] = llm_error
            details["fallback"] = "baseline"
        _finalize_pipeline_market(
            record,
            final_stage="candidate_creation",
            decision="CANDIDATE",
            reason="candidate_generated",
            details=details,
        )

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
    summary["diagnostics_written"] = len(infer_diag_rows)
    summary["finalized_markets"] = sum(
        1 for row in report["markets"] if row["reason"] != "unclassified"
    )
    return (candidate, report)


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
        pipeline_report: Optional[Dict[str, Any]] = None

        if mode == "arbiter":
            cand = _arbiter_candidate_from_db(conn=conn, venue=venue, paper_size=paper_size)
        else:
            cand, pipeline_report = _infer_one(conn=conn, venue=venue, paper_size=paper_size)

        cands: List[Dict[str, Any]] = [cand] if cand is not None else []
        _write_candidates(mode, cands)

        paper_status = ""
        if args.paper and cand is not None:
            paper_status = "paper=" + _insert_paper_trade(conn, cand)

        if pipeline_report is not None:
            if cand is not None:
                market_record = next(
                    row for row in pipeline_report["markets"]
                    if row["market_id"] == cand["market_id"]
                )
                if not args.paper:
                    pipeline_report["summary"]["paper_not_requested"] += 1
                    market_record["details"]["paper_result"] = "not_requested"
                elif paper_status == "paper=queued_for_approval":
                    pipeline_report["summary"]["paper_pending"] += 1
                    market_record["details"]["paper_result"] = "pending_approval"
                elif paper_status == "paper=inserted":
                    pipeline_report["summary"]["paper_inserted"] += 1
                    market_record["details"]["paper_result"] = "inserted_open"
                elif paper_status == "paper=skipped_duplicate":
                    pipeline_report["summary"]["paper_duplicate"] += 1
                    market_record["details"]["paper_result"] = "duplicate"
            _write_pipeline_report(pipeline_report)
            _print_pipeline_funnel(pipeline_report)

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
