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
    from llm.openai_client import (
        openai_enabled,
        forecast_yes_probability,
        get_last_usage,
        review_forecast,
    )
except Exception:
    openai_enabled = lambda: False  # type: ignore
    forecast_yes_probability = None  # type: ignore
    review_forecast = None  # type: ignore
    get_last_usage = lambda: None  # type: ignore

from context.temporal import build_temporal_context, validate_temporal_rationale
from loop_engine.config import LLMBudget, LoopEngineConfig
from loop_engine.opportunity import score_opportunity
from loop_engine.shadow import ensure_shadow_schema, insert_shadow_forecast
from loop_engine.prompts import classify_market
from loop_engine.skeptic import should_request_skeptic
from market_universe.policy import (
    InstitutionalUniverseConfig,
    evaluate_market as evaluate_market_policy,
)
from models.baseline import score_market, market_yes_price
from loop_engine.config import DEFAULT_LLM_USAGE_PATH
from swarm_edge_runtime import RUNTIME_PATHS

DB_PATH = str(RUNTIME_PATHS.db_path)
SIGNALS_DIR = RUNTIME_PATHS.signals_dir
WATCHLIST_PATH = RUNTIME_PATHS.watchlist_path
PIPELINE_REPORT_PATH = SIGNALS_DIR / "infer_pipeline_report.json"
UNIVERSE_REPORT_PATH = RUNTIME_PATHS.report_dir / "phase3_2_universe_expansion.json"

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
    "opportunity_scored",
    "low_opportunity_score",
    "weak_market_quality",
    "banned_market_class",
    "malformed_market",
    "weak_resolution_quality",
    "low_institutional_quality",
    "llm_attempted",
    "llm_failed",
    "budget_skipped",
    "skeptic_attempted",
    "skeptic_failed",
    "skeptic_reject",
    "skeptic_downgrade",
    "edge_rejected",
    "disagreement_rejected",
    "temporal_inconsistency",
    "diagnostics_written",
    "candidates_generated",
    "paper_inserted",
    "paper_pending",
    "paper_duplicate",
    "paper_not_requested",
    "shadow_inserted",
    "shadow_duplicate",
)


def _candidates_path(mode: str) -> Path:
    if mode == "arbiter":
        return SIGNALS_DIR / "trade_candidates_arbiter.json"
    if mode == "infer":
        return SIGNALS_DIR / "trade_candidates_infer.json"
    return SIGNALS_DIR / "trade_candidates.json"


def utc_now_iso(now: Optional[datetime] = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()


def _connect_db(path: str, *, read_only: bool = False) -> sqlite3.Connection:
    if read_only:
        resolved = Path(path).resolve()
        conn = sqlite3.connect(f"file:{resolved}?mode=ro", uri=True)
        conn.execute("PRAGMA query_only=ON;")
    else:
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
                "brain": {
                    "market_id": slug,
                    "question": slug,
                    "category": "novelty/other",
                    "opportunity_score": None,
                    "opportunity_grade": None,
                    "p_yes_market": None,
                    "p_yes_model": None,
                    "edge": None,
                    "llm_used": False,
                    "skeptic_used": False,
                    "temporal_status": "not_evaluated",
                    "budget_status": "not_applicable",
                    "scoring_components": {},
                    "short_rationale_summary": None,
                    "policy_allowed": None,
                    "policy_reason": "not_evaluated",
                    "policy_classification": "UNKNOWN_REQUIRES_REVIEW",
                    "policy_tier": "NOT_EVALUATED",
                    "institutional_category": "novelty/other",
                    "institutional_quality_score": None,
                    "banned_class": None,
                },
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


def _update_brain(record: Dict[str, Any], **values: Any) -> None:
    record.setdefault("brain", {}).update(values)


def _daily_usage_path() -> Path:
    return SIGNALS_DIR / DEFAULT_LLM_USAGE_PATH.name


def _brain_report_path() -> Path:
    return SIGNALS_DIR / "swarm_brain_report.json"


def _build_brain_report(
    report: Dict[str, Any],
    *,
    mode: str,
    budget: LLMBudget,
    config: LoopEngineConfig,
    universe_config: Optional[InstitutionalUniverseConfig] = None,
) -> Dict[str, Any]:
    markets: List[Dict[str, Any]] = []
    for row in report["markets"]:
        brain = dict(row.get("brain") or {})
        brain["final_decision"] = row.get("decision", "SKIP")
        brain["final_reason"] = row.get("reason", "unclassified")
        markets.append(brain)

    rankings = sorted(
        (
            {
                "rank": 0,
                "market_id": row["market_id"],
                "question": row.get("question"),
                "category": row.get("category"),
                "opportunity_score": row.get("opportunity_score"),
                "opportunity_grade": row.get("opportunity_grade"),
                "final_decision": row.get("final_decision"),
                "final_reason": row.get("final_reason"),
            }
            for row in markets
            if row.get("opportunity_score") is not None
            and "liquidity"
            in ((row.get("scoring_components") or {}).get("raw") or {})
        ),
        key=lambda row: float(row["opportunity_score"]),
        reverse=True,
    )
    for index, row in enumerate(rankings, start=1):
        row["rank"] = index

    rejection_distribution: Dict[str, int] = {}
    sampled_tiers: Dict[str, int] = {}
    for row in markets:
        reason = str(row.get("final_reason") or "unclassified")
        rejection_distribution[reason] = (
            rejection_distribution.get(reason, 0) + 1
        )
        if reason == "not_selected_in_batch":
            continue
        tier = str(row.get("policy_tier") or "NOT_EVALUATED")
        sampled_tiers[tier] = sampled_tiers.get(tier, 0) + 1

    watchlist_tiers = dict(sampled_tiers)
    try:
        expansion = json.loads(
            UNIVERSE_REPORT_PATH.read_text(encoding="utf-8")
        )
        configured_tiers = (
            expansion.get("selection_summary", {}).get("tier_counts", {})
        )
        if isinstance(configured_tiers, dict):
            watchlist_tiers = {
                str(key): int(value)
                for key, value in configured_tiers.items()
            }
    except (FileNotFoundError, json.JSONDecodeError, TypeError, ValueError):
        pass

    return {
        "run_id": report["run_id"],
        "ts_utc": report["ts_utc"],
        "mode": mode,
        "watchlist_total": report["summary"]["watchlist_total"],
        "sampled_markets": report["summary"]["fetch_attempted"],
        "opportunity_rankings": rankings,
        "llm_calls_used": budget.llm_calls_used,
        "skeptic_calls_used": budget.skeptic_calls_used,
        "daily_llm_calls_used": budget.daily_calls_used,
        "estimated_cost": budget.estimated_cost,
        "budget_config": config.as_dict(),
        "market_universe_policy_mode": (
            universe_config.mode if universe_config else "unknown"
        ),
        "watchlist_tier_counts": watchlist_tiers,
        "sampled_tier_counts": sampled_tiers,
        "rejection_distribution_summary": rejection_distribution,
        "market_records": markets,
    }


def _write_brain_report(payload: Dict[str, Any]) -> None:
    SIGNALS_DIR.mkdir(parents=True, exist_ok=True)
    _brain_report_path().write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


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
        + s.get("low_opportunity_score", 0)
        + s.get("skeptic_reject", 0)
        + s.get("banned_market_class", 0)
        + s.get("malformed_market", 0)
        + s.get("weak_resolution_quality", 0)
        + s.get("low_institutional_quality", 0)
    )
    print(
        "PIPELINE "
        f"watchlist={s['watchlist_total']} fetched={s['fetch_attempted'] - s['fetch_failed']} "
        f"ranked={s.get('opportunity_scored', 0)} llm={s['llm_attempted']} "
        f"skeptic={s.get('skeptic_attempted', 0)} rejected={rejected_total} "
        f"candidates={s['candidates_generated']}",
        flush=True,
    )
    print(
        "SKIPS "
        f"existing={s['blocked_existing_position']} category={s['skipped_category_cap']} "
        f"fetch_failed={s['fetch_failed']} inactive={s['inactive_or_closed']} "
        f"quality={s.get('weak_market_quality', 0)} opportunity={s.get('low_opportunity_score', 0)} "
        f"policy={s.get('banned_market_class', 0) + s.get('malformed_market', 0) + s.get('weak_resolution_quality', 0) + s.get('low_institutional_quality', 0)} "
        f"budget={s.get('budget_skipped', 0)} edge={s['edge_rejected']} "
        f"temporal={s.get('temporal_inconsistency', 0)}",
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
    min_edge_abs = _env_float("BGL_MIN_EDGE_ABS", _env_float("BGL_MIN_EDGE", 0.04))
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
    """Classify market question for ranking, prompts, and concentration tracking."""
    return classify_market(question)


def _category_exposure_count(conn: sqlite3.Connection, category: str) -> int:
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
    return count


def _category_cap_ok(conn: sqlite3.Connection, category: str) -> bool:
    """Return True if opening another position in this category is within the cap."""
    max_per = int(os.environ.get("BGL_MAX_PER_CATEGORY", "3") or "3")
    return _category_exposure_count(conn, category) < max_per


def _infer_one(
    *,
    conn: sqlite3.Connection,
    venue: str,
    paper_size: float,
    persist_state: bool = False,
    paper_mode: bool = False,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    watchlist = _load_watchlist()
    report = _new_pipeline_report(watchlist, venue)
    records = {row["market_id"]: row for row in report["markets"]}
    summary = report["summary"]
    config = LoopEngineConfig.from_env()
    universe_config = InstitutionalUniverseConfig.from_env()
    budget = LLMBudget(config, _daily_usage_path())
    default_batch = max(config.evaluations_per_cycle * 2, config.evaluations_per_cycle)
    shadow_backup_path: Optional[str] = None
    if paper_mode and config.shadow_ledger_enabled:
        db_row = conn.execute("PRAGMA database_list").fetchone()
        db_path = str(db_row[2]) if db_row and db_row[2] else ":memory:"
        backup_dir = RUNTIME_PATHS.backup_dir
        backup_path = ensure_shadow_schema(
            conn,
            db_path=db_path,
            backup_dir=backup_dir,
        )
        shadow_backup_path = str(backup_path) if backup_path else None
    infer_diag_rows: List[Dict[str, Any]] = []
    infer_diag_counts: Dict[str, Any] = {
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
            "low_opportunity_score": 0,
            "weak_market_quality": 0,
            "budget_skipped": 0,
            "skeptic_reject": 0,
            "skeptic_downgrade": 0,
            "banned_market_class": 0,
            "malformed_market": 0,
            "weak_resolution_quality": 0,
            "low_institutional_quality": 0,
        },
    }

    def count_rejection(reason: str) -> None:
        rejected = infer_diag_counts["rejected"]
        rejected[reason] = int(rejected.get(reason, 0)) + 1

    def record_shadow(
        *,
        item: Dict[str, Any],
        model_probability: float,
        edge_abs: float,
        side: str,
        production_decision: str,
        rejection_reason: Optional[str],
        temporal_validation: str,
        llm_used: bool,
        skeptic_result: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not paper_mode or not config.shadow_ledger_enabled:
            return
        opportunity = item["opportunity"]
        record = item["record"]
        policy_quality = record.get("brain", {}).get(
            "institutional_quality_score"
        )
        production_threshold = _filters()[0]
        result = insert_shadow_forecast(
            conn,
            {
                "run_id": report["run_id"],
                "timestamp_utc": report["ts_utc"],
                "venue": venue,
                "market_id": item["slug"],
                "slug": item["slug"],
                "question": item["question"],
                "category": item["category"],
                "market_probability": item["p_yes_market"],
                "model_probability": model_probability,
                "side": side,
                "absolute_edge": edge_abs,
                "opportunity_score": opportunity.opportunity_score,
                "quality_score": policy_quality,
                "grade": opportunity.opportunity_grade,
                "contract_validity": "valid",
                "opportunity_quality": "qualified",
                "model_edge": (
                    "meets_production_threshold"
                    if edge_abs >= production_threshold
                    else "below_production_threshold"
                ),
                "production_decision": production_decision,
                "rejection_reason": rejection_reason,
                "temporal_validation": temporal_validation,
                "skeptic_result": skeptic_result,
                "market_end_date": item["temporal_context"].get(
                    "market_end_date"
                ),
                "llm_used": llm_used,
                "model_name": (
                    os.environ.get("BGL_LLM_MODEL", "")
                    if llm_used
                    else "baseline"
                ),
                "metadata": {
                    "pricing_source": item["pricing_source"],
                    "spread": item["spread"],
                    "market_snapshot_id": item["market"].get("id"),
                    "market_snapshot": {
                        "best_bid": item["market"].get("bestBid"),
                        "best_ask": item["market"].get("bestAsk"),
                        "liquidity": item["market"].get("liquidity"),
                        "volume": item["market"].get("volume"),
                        "updatedAt": item["market"].get("updatedAt"),
                        "feesEnabled": item["market"].get("feesEnabled"),
                        "feeRate": item["market"].get("feeRate"),
                    },
                    "temporal_context": item["temporal_context"],
                    "scoring_components": opportunity.scoring_components,
                    "anthropic_usage": item.get("anthropic_usage_events") or None,
                },
            },
            thresholds=config.threshold_buckets,
            production_threshold=production_threshold,
            hypothetical_stake_usd=paper_size,
        )
        summary["shadow_inserted" if result.inserted else "shadow_duplicate"] += 1

    def finish(
        candidate: Optional[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
        _write_infer_diagnostics(
            {
                "ts_utc": utc_now_iso(),
                "source": venue,
                "mode": "infer",
                "settings": {
                    "batch": int(
                        os.environ.get(
                            "BGL_INFER_BATCH", str(default_batch)
                        )
                        or str(default_batch)
                    ),
                    "cooldown": int(
                        os.environ.get("BGL_INFER_COOLDOWN", "0") or "0"
                    ),
                    "min_edge_abs": _filters()[0],
                    "min_edge_vs_market": _filters()[1],
                    "max_disagree": _filters()[2],
                    "paper_size": paper_size,
                    **config.as_dict(),
                    "market_universe_policy": universe_config.as_dict(),
                },
                "summary": infer_diag_counts,
                "rows": infer_diag_rows,
            }
        )
        summary["diagnostics_written"] = len(infer_diag_rows)
        summary["finalized_markets"] = sum(
            1 for row in report["markets"] if row["reason"] != "unclassified"
        )
        brain_report = _build_brain_report(
            report,
            mode="paper_research" if paper_mode else "research",
            budget=budget,
            config=config,
            universe_config=universe_config,
        )
        report["market_universe"] = {
            "policy_mode": universe_config.mode,
            "watchlist_tier_counts": brain_report["watchlist_tier_counts"],
            "sampled_tier_counts": brain_report["sampled_tier_counts"],
            "rejection_distribution_summary": brain_report[
                "rejection_distribution_summary"
            ],
        }
        report["brain_report"] = brain_report
        report["shadow_ledger"] = {
            "enabled": config.shadow_ledger_enabled,
            "inserted": summary["shadow_inserted"],
            "duplicates": summary["shadow_duplicate"],
            "migration_backup_path": shadow_backup_path,
        }
        _write_brain_report(brain_report)
        return candidate, report

    if not watchlist or get_adapter is None:
        reason = "empty_watchlist" if not watchlist else "adapter_unavailable"
        for record in report["markets"]:
            _finalize_pipeline_market(
                record,
                final_stage="setup",
                decision="SKIP",
                reason=reason,
            )
        return finish(None)

    batch = int(
        os.environ.get(
            "BGL_INFER_BATCH", str(default_batch)
        )
        or str(default_batch)
    )
    cooldown_n = int(os.environ.get("BGL_INFER_COOLDOWN", "0") or "0")

    slugs, next_cursor = _infer_pick_slugs_batch(conn, watchlist, batch)
    if persist_state:
        _kv_set(conn, "infer_cursor", str(next_cursor))
        conn.commit()

    recent = set(_infer_recent_slugs(conn, venue, cooldown_n))
    existing = _existing_position_slugs(conn, venue)
    adapter = get_adapter(venue)

    selected = set(slugs)
    for slug in watchlist:
        record = records[slug]
        if slug in existing:
            summary["blocked_existing_position"] += 1
            _update_brain(
                record,
                opportunity_score=0.0,
                opportunity_grade="F",
                budget_status="not_eligible",
                scoring_components={
                    "raw": {
                        "duplicate_position": True,
                        "existing_exposure": True,
                    }
                },
            )
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

    ranked: List[Dict[str, Any]] = []
    for slug in slugs:
        record = records[slug]
        if slug in existing:
            continue
        if cooldown_n > 0 and slug in recent:
            _update_brain(
                record,
                opportunity_score=0.0,
                opportunity_grade="F",
                budget_status="not_eligible",
                scoring_components={"raw": {"recent_cooldown": True}},
            )
            _finalize_pipeline_market(
                record,
                final_stage="cooldown_filter",
                decision="SKIP",
                reason="recent_infer_cooldown",
                details={"cooldown": cooldown_n},
            )
            continue

        summary["fetch_attempted"] += 1
        try:
            m = adapter.get_market(slug)  # type: ignore[attr-defined]
        except Exception as e:
            summary["fetch_failed"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection("fetch_failed")
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

        question = str(m.get("question") or slug)
        category = _topic_label(question)
        _update_brain(record, question=question, category=category)

        policy = evaluate_market_policy(
            m,
            config=universe_config,
            duplicate_position=False,
        )
        _update_brain(
            record,
            policy_allowed=policy.policy_allowed,
            policy_reason=policy.policy_reason,
            policy_classification=policy.classification,
            policy_tier=policy.policy_tier,
            institutional_category=policy.institutional_category,
            institutional_quality_score=policy.institutional_quality_score,
            banned_class=policy.banned_class,
        )
        if not policy.policy_allowed:
            reason = policy.policy_reason
            if reason not in {
                "banned_market_class",
                "malformed_market",
                "weak_resolution_quality",
                "low_institutional_quality",
            }:
                reason = "low_institutional_quality"
            summary[reason] += 1
            summary["weak_market_quality"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection(reason)
            infer_diag_rows.append(
                {
                    "slug": slug,
                    "question": question,
                    "decision": "REJECT",
                    "reason": reason,
                    "policy": policy.as_dict(),
                }
            )
            _update_brain(
                record,
                opportunity_score=0.0,
                opportunity_grade="F",
                budget_status="not_eligible",
                scoring_components={
                    "policy": policy.as_dict(),
                    "raw": policy.metrics,
                },
            )
            _finalize_pipeline_market(
                record,
                final_stage="market_universe_policy",
                decision="REJECT",
                reason=reason,
                details={"policy": policy.as_dict()},
            )
            continue

        temporal_context = build_temporal_context(
            m,
            question=question,
            slug=slug,
        )
        _update_brain(
            record,
            temporal_status=str(temporal_context.get("event_status") or "UNKNOWN"),
        )
        if temporal_context.get("requires_verified_temporal_context") and temporal_context.get("event_status") == "UNKNOWN":
            summary["temporal_inconsistency"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection("temporal_inconsistency")
            infer_diag_rows.append({
                "slug": slug,
                "question": question,
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
            count_rejection("invalid_price")
            infer_diag_rows.append({
                "slug": slug,
                "question": question,
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

        _update_brain(record, p_yes_market=float(p_yes_market))
        min_crowd = _env_float("BGL_MIN_CROWD_PRICE", 0.03)
        max_crowd = 1.0 - min_crowd
        if p_yes_market < min_crowd or p_yes_market > max_crowd:
            summary["extreme_tail"] += 1
            summary["weak_market_quality"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection("extreme_tail")
            infer_diag_rows.append({
                "slug": slug, "decision": "REJECT",
                "reason": "weak_market_quality",
                "quality_reason": "extreme_tail",
                "p_yes_market": float(p_yes_market),
            })
            _finalize_pipeline_market(
                record,
                final_stage="tail_filter",
                decision="REJECT",
                reason="weak_market_quality",
                details={
                    "quality_reason": "extreme_tail",
                    "p_yes_market": float(p_yes_market),
                },
            )
            continue

        baseline = score_market(m)
        category_exposure = _category_exposure_count(conn, category)
        opportunity = score_opportunity(
            m,
            category=category,
            p_yes_market=p_yes_market,
            spread=spread,
            temporal_context=temporal_context,
            min_score_for_llm=config.min_opportunity_score_for_llm,
            existing_exposure=category_exposure > 0,
            quality_reject_reason=baseline.reject_reason,
            time_to_resolution_weight=config.time_to_resolution_weight,
        )
        summary["opportunity_scored"] += 1
        _update_brain(
            record,
            opportunity_score=opportunity.opportunity_score,
            opportunity_grade=opportunity.opportunity_grade,
            scoring_components=opportunity.scoring_components,
        )
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
            summary["weak_market_quality"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection(baseline.reject_reason)
            count_rejection("weak_market_quality")
            infer_diag_rows.append({
                "slug": slug,
                "question": question,
                "p_yes_market": float(baseline.p_yes_market),
                "p_yes_model": float(baseline.p_yes_model),
                "edge_vs_market": float(baseline.p_yes_model - baseline.p_yes_market),
                "edge_abs": float(abs(baseline.p_yes_model - 0.5)),
                "disagreement": 1.0,
                "side": "YES" if baseline.p_yes_model >= 0.5 else "NO",
                "decision": "REJECT",
                "reason": "weak_market_quality",
                "quality_reason": baseline.reject_reason,
                "pricing_source": pricing_source,
                "spread": float(spread),
                "components": baseline.components,
            })
            _finalize_pipeline_market(
                record,
                final_stage="market_quality_filter",
                decision="REJECT",
                reason="weak_market_quality",
                details={
                    "quality_reason": baseline.reject_reason,
                    "p_yes_market": float(baseline.p_yes_market),
                    "spread": float(spread),
                    "pricing_source": pricing_source,
                    "opportunity_score": opportunity.opportunity_score,
                },
            )
            continue

        if not opportunity.eligible_for_llm:
            summary["low_opportunity_score"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection("low_opportunity_score")
            infer_diag_rows.append(
                {
                    "slug": slug,
                    "question": question,
                    "decision": "REJECT",
                    "reason": "low_opportunity_score",
                    "opportunity_score": opportunity.opportunity_score,
                    "opportunity_grade": opportunity.opportunity_grade,
                    "components": opportunity.scoring_components,
                }
            )
            _finalize_pipeline_market(
                record,
                final_stage="opportunity_ranker",
                decision="REJECT",
                reason="low_opportunity_score",
                details={
                    "opportunity_score": opportunity.opportunity_score,
                    "minimum": config.min_opportunity_score_for_llm,
                },
            )
            continue

        if not _category_cap_ok(conn, category):
            summary["skipped_category_cap"] += 1
            infer_diag_counts["evaluated"] += 1
            count_rejection("category_cap")
            infer_diag_rows.append(
                {
                    "slug": slug,
                    "question": question,
                    "decision": "REJECT",
                    "reason": "category_cap",
                    "category": category,
                    "opportunity_score": opportunity.opportunity_score,
                }
            )
            print(f"  [infer] category cap reached for '{category}' - skipping {slug}", flush=True)
            _finalize_pipeline_market(
                record,
                final_stage="category_cap",
                decision="SKIP",
                reason="category_cap_reached",
                details={"category": category},
            )
            continue

        ranked.append(
            {
                "slug": slug,
                "record": record,
                "market": m,
                "question": question,
                "category": category,
                "temporal_context": temporal_context,
                "p_yes_market": float(p_yes_market),
                "spread": float(spread),
                "pricing_source": pricing_source,
                "baseline": baseline,
                "opportunity": opportunity,
            }
        )

    ranked.sort(
        key=lambda item: item["opportunity"].opportunity_score,
        reverse=True,
    )
    for item in ranked[config.evaluations_per_cycle :]:
        _finalize_pipeline_market(
            item["record"],
            final_stage="evaluation_limit",
            decision="SKIP",
            reason="evaluation_limit_reached",
            details={"evaluations_per_cycle": config.evaluations_per_cycle},
        )
    ranked = ranked[: config.evaluations_per_cycle]
    use_llm = (
        _env_bool("BGL_INFER_USE_LLM", False)
        and openai_enabled()
        and forecast_yes_probability is not None
    )
    candidate: Optional[Dict[str, Any]] = None

    for item in ranked:
        slug = item["slug"]
        record = item["record"]
        m = item["market"]
        question = item["question"]
        category = item["category"]
        temporal_context = item["temporal_context"]
        p_yes_market = item["p_yes_market"]
        spread = item["spread"]
        pricing_source = item["pricing_source"]
        baseline = item["baseline"]
        opportunity = item["opportunity"]

        llm_rationale = ""
        llm_conf = float(baseline.confidence)
        p_yes_model = float(baseline.p_yes_model)
        disagreement = float(max(0.0, min(1.0, 1.0 - baseline.confidence)))
        components = baseline.components
        llm_used = False
        llm_error = ""
        skeptic_used = False
        skeptic_payload: Dict[str, Any] = {}
        item["anthropic_usage_events"] = []

        if use_llm:
            if not budget.reserve_primary():
                summary["budget_skipped"] += 1
                infer_diag_counts["evaluated"] += 1
                count_rejection("budget_skipped")
                budget_status = budget.primary_status()
                _update_brain(record, budget_status=budget_status)
                baseline_edge = float(baseline.p_yes_model - p_yes_market)
                record_shadow(
                    item=item,
                    model_probability=float(baseline.p_yes_model),
                    edge_abs=abs(baseline_edge),
                    side="YES" if baseline_edge > 0 else "NO",
                    production_decision="not_evaluated_for_production",
                    rejection_reason="budget_skipped",
                    temporal_validation=str(
                        temporal_context.get("event_status") or "UNKNOWN"
                    ),
                    llm_used=False,
                )
                infer_diag_rows.append(
                    {
                        "slug": slug,
                        "question": question,
                        "decision": "SKIP",
                        "reason": "budget_skipped",
                        "budget_status": budget_status,
                        "opportunity_score": opportunity.opportunity_score,
                    }
                )
                _finalize_pipeline_market(
                    record,
                    final_stage="llm_budget",
                    decision="SKIP",
                    reason="budget_skipped",
                    details={"budget_status": budget_status},
                )
                continue

            summary["llm_attempted"] += 1
            _update_brain(record, budget_status="llm_reserved")
            ctx = {
                "venue": venue,
                "slug": slug,
                "category": category,
                "p_yes_market": p_yes_market,
                "temporal_context": temporal_context,
                "market_snapshot": {
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "updatedAt": m.get("updatedAt"),
                    "startDate": m.get("startDate"),
                    "endDate": m.get("endDate"),
                    "resolutionDate": m.get("resolutionDate"),
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
                    question=question,
                    context=ctx,
                )
                if get_last_usage() is not None:
                    item["anthropic_usage_events"].append(get_last_usage())
                p_yes_model = float(min(0.99, max(0.01, p_yes_model)))
                llm_conf = float(min(0.95, max(0.0, llm_conf)))
                disagreement = float(max(0.0, min(1.0, 1.0 - llm_conf)))
                components = {}
                llm_used = True
                _update_brain(
                    record,
                    llm_used=True,
                    budget_status="llm_used",
                    short_rationale_summary=llm_rationale[:240] or None,
                )
            except Exception as llm_err:
                if get_last_usage() is not None:
                    item["anthropic_usage_events"].append(get_last_usage())
                llm_error = str(llm_err)[:500]
                summary["llm_failed"] += 1
                _update_brain(record, budget_status="llm_failed")
                print(
                    f"[WARN] LLM call failed, falling back to baseline: {llm_error[:120]}",
                    flush=True,
                )
                if "billing" in llm_error.lower() or "credit" in llm_error.lower():
                    use_llm = False

        if llm_used:
            valid_temporal, temporal_reason, temporal_details = validate_temporal_rationale(
                llm_rationale,
                temporal_context,
                question=question,
            )
            if not valid_temporal:
                summary["temporal_inconsistency"] += 1
                infer_diag_counts["evaluated"] += 1
                count_rejection("temporal_inconsistency")
                edge_vs_market = float(p_yes_model - p_yes_market)
                _update_brain(
                    record,
                    p_yes_model=p_yes_model,
                    edge=abs(edge_vs_market),
                    temporal_status=temporal_reason,
                )
                infer_diag_rows.append(
                    {
                        "slug": slug,
                        "question": question,
                        "p_yes_market": p_yes_market,
                        "p_yes_model": p_yes_model,
                        "edge_vs_market": edge_vs_market,
                        "edge_abs": abs(edge_vs_market),
                        "disagreement": disagreement,
                        "llm_confidence": llm_conf,
                        "llm_rationale": llm_rationale,
                        "llm_used": True,
                        "decision": "REJECT",
                        "reason": "temporal_inconsistency",
                        "temporal_validation": temporal_reason,
                        "temporal_details": temporal_details,
                    }
                )
                record_shadow(
                    item=item,
                    model_probability=p_yes_model,
                    edge_abs=abs(edge_vs_market),
                    side="YES" if edge_vs_market > 0 else "NO",
                    production_decision="rejected",
                    rejection_reason="temporal_inconsistency",
                    temporal_validation=temporal_reason,
                    llm_used=True,
                )
                _finalize_pipeline_market(
                    record,
                    final_stage="temporal_validation",
                    decision="REJECT",
                    reason="temporal_inconsistency",
                    details={
                        "temporal_validation": temporal_reason,
                        "temporal_details": temporal_details,
                        "llm_used": True,
                    },
                )
                continue

        edge_vs_market = float(p_yes_model - p_yes_market)
        edge_abs = abs(edge_vs_market)
        reason = _infer_rejection_reason(
            edge_abs=edge_abs,
            edge_vs_market=edge_vs_market,
            disagreement=disagreement,
        )

        skeptic_trigger, skeptic_trigger_reason = should_request_skeptic(
            edge_abs=edge_abs,
            confidence=llm_conf,
            category=category,
            temporal_context=temporal_context,
            candidate_threshold=config.candidate_threshold_for_skeptic,
            near_threshold_ratio=config.skeptic_near_threshold_ratio,
            high_confidence=config.skeptic_high_confidence,
        )
        if llm_used and skeptic_trigger:
            if review_forecast is None or not budget.reserve_skeptic():
                summary["budget_skipped"] += 1
                infer_diag_counts["evaluated"] += 1
                count_rejection("budget_skipped")
                budget_status = (
                    "skeptic_unavailable"
                    if review_forecast is None
                    else budget.skeptic_status()
                )
                _update_brain(
                    record,
                    p_yes_model=p_yes_model,
                    edge=edge_abs,
                    budget_status=budget_status,
                )
                record_shadow(
                    item=item,
                    model_probability=p_yes_model,
                    edge_abs=edge_abs,
                    side="YES" if edge_vs_market > 0 else "NO",
                    production_decision="not_evaluated_for_production",
                    rejection_reason="budget_skipped",
                    temporal_validation="valid",
                    llm_used=llm_used,
                    skeptic_result={
                        "action": "NOT_RUN",
                        "reason": budget_status,
                        "trigger": skeptic_trigger_reason,
                    },
                )
                infer_diag_rows.append(
                    {
                        "slug": slug,
                        "question": question,
                        "decision": "SKIP",
                        "reason": "budget_skipped",
                        "budget_status": budget_status,
                        "skeptic_trigger": skeptic_trigger_reason,
                    }
                )
                _finalize_pipeline_market(
                    record,
                    final_stage="skeptic_budget",
                    decision="SKIP",
                    reason="budget_skipped",
                    details={
                        "budget_status": budget_status,
                        "skeptic_trigger": skeptic_trigger_reason,
                    },
                )
                continue

            summary["skeptic_attempted"] += 1
            skeptic_used = True
            _update_brain(record, skeptic_used=True, budget_status="skeptic_reserved")
            try:
                review = review_forecast(
                    question=question,
                    category=category,
                    p_yes_market=p_yes_market,
                    p_yes_model=p_yes_model,
                    confidence=llm_conf,
                    rationale=llm_rationale,
                    temporal_context=temporal_context,
                )
                if get_last_usage() is not None:
                    item["anthropic_usage_events"].append(get_last_usage())
                skeptic_payload = {
                    "action": review.action,
                    "reason": review.reason,
                    "rationale": review.rationale,
                    "temporal_valid": review.temporal_valid,
                    "stale_facts": review.stale_facts,
                    "malformed_or_novelty": review.malformed_or_novelty,
                    "edge_real": review.edge_real,
                    "trigger": skeptic_trigger_reason,
                }
            except Exception as skeptic_err:
                if get_last_usage() is not None:
                    item["anthropic_usage_events"].append(get_last_usage())
                summary["skeptic_failed"] += 1
                summary["skeptic_reject"] += 1
                infer_diag_counts["evaluated"] += 1
                count_rejection("skeptic_reject")
                skeptic_payload = {
                    "action": "REJECT",
                    "reason": "critic_call_failed",
                    "rationale": str(skeptic_err)[:300],
                    "trigger": skeptic_trigger_reason,
                }
                _update_brain(
                    record,
                    p_yes_model=p_yes_model,
                    edge=edge_abs,
                    budget_status="skeptic_failed",
                )
                record_shadow(
                    item=item,
                    model_probability=p_yes_model,
                    edge_abs=edge_abs,
                    side="YES" if edge_vs_market > 0 else "NO",
                    production_decision="rejected",
                    rejection_reason="skeptic_reject",
                    temporal_validation="valid",
                    llm_used=llm_used,
                    skeptic_result=skeptic_payload,
                )
                infer_diag_rows.append(
                    {
                        "slug": slug,
                        "question": question,
                        "decision": "REJECT",
                        "reason": "skeptic_reject",
                        "skeptic": skeptic_payload,
                    }
                )
                _finalize_pipeline_market(
                    record,
                    final_stage="skeptic_review",
                    decision="REJECT",
                    reason="skeptic_reject",
                    details={"skeptic": skeptic_payload},
                )
                continue

            if review.action == "REJECT":
                summary["skeptic_reject"] += 1
                infer_diag_counts["evaluated"] += 1
                count_rejection("skeptic_reject")
                _update_brain(
                    record,
                    p_yes_model=p_yes_model,
                    edge=edge_abs,
                    budget_status="skeptic_used",
                )
                record_shadow(
                    item=item,
                    model_probability=p_yes_model,
                    edge_abs=edge_abs,
                    side="YES" if edge_vs_market > 0 else "NO",
                    production_decision="rejected",
                    rejection_reason="skeptic_reject",
                    temporal_validation="valid",
                    llm_used=llm_used,
                    skeptic_result=skeptic_payload,
                )
                infer_diag_rows.append(
                    {
                        "slug": slug,
                        "question": question,
                        "decision": "REJECT",
                        "reason": "skeptic_reject",
                        "skeptic": skeptic_payload,
                    }
                )
                _finalize_pipeline_market(
                    record,
                    final_stage="skeptic_review",
                    decision="REJECT",
                    reason="skeptic_reject",
                    details={"skeptic": skeptic_payload},
                )
                continue
            if review.action == "DOWNGRADE":
                summary["skeptic_downgrade"] += 1
                p_yes_model = (p_yes_model + p_yes_market) / 2.0
                llm_conf = max(0.5, llm_conf - 0.15)
                disagreement = max(disagreement, 1.0 - llm_conf)
                edge_vs_market = float(p_yes_model - p_yes_market)
                edge_abs = abs(edge_vs_market)
                reason = _infer_rejection_reason(
                    edge_abs=edge_abs,
                    edge_vs_market=edge_vs_market,
                    disagreement=disagreement,
                )
                if reason != "pass":
                    infer_diag_counts["evaluated"] += 1
                    count_rejection("skeptic_downgrade")
                    _update_brain(
                        record,
                        p_yes_model=p_yes_model,
                        edge=edge_abs,
                        budget_status="skeptic_used",
                    )
                    record_shadow(
                        item=item,
                        model_probability=p_yes_model,
                        edge_abs=edge_abs,
                        side="YES" if edge_vs_market > 0 else "NO",
                        production_decision="rejected",
                        rejection_reason="skeptic_downgrade",
                        temporal_validation="valid",
                        llm_used=llm_used,
                        skeptic_result=skeptic_payload,
                    )
                    infer_diag_rows.append(
                        {
                            "slug": slug,
                            "question": question,
                            "decision": "REJECT",
                            "reason": "skeptic_downgrade",
                            "post_downgrade_filter": reason,
                            "skeptic": skeptic_payload,
                        }
                    )
                    _finalize_pipeline_market(
                        record,
                        final_stage="skeptic_review",
                        decision="REJECT",
                        reason="skeptic_downgrade",
                        details={
                            "post_downgrade_filter": reason,
                            "skeptic": skeptic_payload,
                        },
                    )
                    continue
            _update_brain(record, budget_status="skeptic_used")

        side = "YES" if edge_vs_market > 0 else "NO"
        infer_diag_counts["evaluated"] += 1
        diag_row = {
            "slug": slug,
            "question": question,
            "category": category,
            "opportunity_score": opportunity.opportunity_score,
            "opportunity_grade": opportunity.opportunity_grade,
            "p_yes_market": p_yes_market,
            "p_yes_model": p_yes_model,
            "edge_vs_market": edge_vs_market,
            "edge_abs": edge_abs,
            "disagreement": disagreement,
            "llm_confidence": llm_conf,
            "llm_rationale": llm_rationale,
            "llm_used": llm_used,
            "skeptic_used": skeptic_used,
            "skeptic": skeptic_payload or None,
            "side": side,
            "decision": "PASS" if reason == "pass" else "REJECT",
            "reason": reason,
            "pricing_source": pricing_source,
            "spread": spread,
            "components": components,
            "scoring_components": opportunity.scoring_components,
            "temporal_context": temporal_context,
        }
        infer_diag_rows.append(diag_row)
        _update_brain(
            record,
            p_yes_model=p_yes_model,
            edge=edge_abs,
            llm_used=llm_used,
            skeptic_used=skeptic_used,
        )

        production_selected = reason == "pass" and candidate is None
        record_shadow(
            item=item,
            model_probability=p_yes_model,
            edge_abs=edge_abs,
            side=side,
            production_decision=(
                "candidate_pending_approval"
                if production_selected
                else ("candidate_limit_reached" if reason == "pass" else "rejected")
            ),
            rejection_reason=(
                None
                if production_selected
                else ("candidate_limit_reached" if reason == "pass" else reason)
            ),
            temporal_validation="valid",
            llm_used=llm_used,
            skeptic_result=skeptic_payload or None,
        )

        if reason != "pass":
            count_rejection(reason)
            if reason == "max_disagree":
                summary["disagreement_rejected"] += 1
            else:
                summary["edge_rejected"] += 1
            details = {
                "p_yes_market": p_yes_market,
                "p_yes_model": p_yes_model,
                "edge_vs_market": edge_vs_market,
                "disagreement": disagreement,
                "llm_used": llm_used,
                "skeptic_used": skeptic_used,
            }
            if llm_error:
                details["llm_error"] = llm_error
                details["fallback"] = "baseline"
            _finalize_pipeline_market(
                record,
                final_stage=(
                    "disagreement_filter" if reason == "max_disagree" else "edge_filter"
                ),
                decision="REJECT",
                reason=reason,
                details=details,
            )
            continue

        next_candidate = {
            "ts_utc": utc_now_iso(),
            "run_id": report["run_id"],
            "market_id": slug,
            "question": question,
            "venue": venue,
            "side": side,
            "p_yes": p_yes_model,
            "consensus_p_yes": p_yes_model,
            "disagreement": disagreement,
            "edge": edge_abs,
            "size_usd": paper_size,
            "reason": "infer",
            "status": "OPEN",
            "notes": {
                "category": category,
                "adapter_venue": venue,
                "p_yes_market": p_yes_market,
                "edge_vs_market": edge_vs_market,
                "pricing_source": pricing_source,
                "spread": spread,
                "opportunity_score": opportunity.opportunity_score,
                "opportunity_grade": opportunity.opportunity_grade,
                "scoring_components": opportunity.scoring_components,
                "llm": {
                    "enabled": use_llm,
                    "used": llm_used,
                    "model": os.environ.get("BGL_LLM_MODEL", ""),
                    "confidence": llm_conf,
                    "rationale": llm_rationale,
                },
                "skeptic": skeptic_payload,
                "baseline_components": components,
                "snapshot": {
                    "slug": slug,
                    "id": m.get("id"),
                    "question": m.get("question"),
                    "updatedAt": m.get("updatedAt"),
                    "endDate": m.get("endDate"),
                    "volume": m.get("volume"),
                    "liquidity": m.get("liquidity"),
                    "bestBid": m.get("bestBid"),
                    "bestAsk": m.get("bestAsk"),
                    "lastTradePrice": m.get("lastTradePrice"),
                },
            },
        }
        infer_diag_counts["passed"] += 1
        if candidate is not None:
            _finalize_pipeline_market(
                record,
                final_stage="candidate_limit",
                decision="SHADOW",
                reason="candidate_limit_reached",
                details={"max_candidates_per_run": 1},
            )
            continue
        candidate = next_candidate
        summary["candidates_generated"] += 1
        _finalize_pipeline_market(
            record,
            final_stage="candidate_creation",
            decision="CANDIDATE",
            reason="candidate_generated",
            details={
                "category": category,
                "side": side,
                "edge_vs_market": edge_vs_market,
                "disagreement": disagreement,
                "llm_used": llm_used,
                "skeptic_used": skeptic_used,
                "skeptic_action": skeptic_payload.get("action"),
            },
        )

    return finish(candidate)


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

    conn = _connect_db(args.db, read_only=not args.paper)

    for i in range(int(args.loops)):
        cand: Optional[Dict[str, Any]] = None
        pipeline_report: Optional[Dict[str, Any]] = None

        if mode == "arbiter":
            cand = _arbiter_candidate_from_db(conn=conn, venue=venue, paper_size=paper_size)
        else:
            cand, pipeline_report = _infer_one(
                conn=conn,
                venue=venue,
                paper_size=paper_size,
                persist_state=bool(args.paper),
                paper_mode=bool(args.paper),
            )

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
