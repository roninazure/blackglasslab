#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import random
import sqlite3
from collections import Counter
from pathlib import Path
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from swarm_edge_runtime import RUNTIME_PATHS


DEFAULT_DB = RUNTIME_PATHS.db_path
DEFAULT_OUTPUT_DIR = RUNTIME_PATHS.report_dir
METHOD_VERSION = "phase2_calibration_v1"
BOOTSTRAP_SEED = 20260711
BOOTSTRAP_SAMPLES = 2000


def _safe_float(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _probability(value: Any) -> Optional[float]:
    result = _safe_float(value)
    if result is None or result < 0.0 or result > 1.0:
        return None
    return result


def _json_objects(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, str) or not value.strip():
        return []
    decoder = json.JSONDecoder()
    objects: List[Dict[str, Any]] = []
    index = 0
    while index < len(value):
        while index < len(value) and value[index].isspace():
            index += 1
        if index >= len(value):
            break
        try:
            parsed, end = decoder.raw_decode(value, index)
        except json.JSONDecodeError:
            newline = value.find("\n", index)
            if newline < 0:
                break
            index = newline + 1
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
        index = end
    return objects


def _notes_parts(value: Any) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    objects = _json_objects(value)
    base = objects[0] if objects else {}
    resolution: Dict[str, Any] = {}
    for obj in objects:
        candidate = obj.get("resolution")
        if isinstance(candidate, dict):
            resolution = candidate
    return base, resolution


def brier_score(probability_yes: float, outcome_yes: int) -> float:
    return (float(probability_yes) - float(outcome_yes)) ** 2


def log_loss(probability_yes: float, outcome_yes: int) -> float:
    p = min(1.0 - 1e-15, max(1e-15, float(probability_yes)))
    y = float(outcome_yes)
    return -(y * math.log(p) + (1.0 - y) * math.log(1.0 - p))


def brier_skill_score(model_brier: Optional[float], market_brier: Optional[float]) -> Optional[float]:
    if model_brier is None or market_brier is None or market_brier == 0.0:
        return None
    return 1.0 - (float(model_brier) / float(market_brier))


def theoretical_profit_usd(
    side: str,
    size_usd: float,
    outcome: str,
    p_yes_market_entry: Optional[float],
) -> float:
    side_up = str(side).upper()
    outcome_up = str(outcome).upper()
    correct = (side_up == "YES" and outcome_up == "YES") or (
        side_up == "NO" and outcome_up == "NO"
    )
    if not correct:
        return -float(size_usd)
    if p_yes_market_entry is None:
        return float(size_usd)
    p = max(0.001, min(0.999, float(p_yes_market_entry)))
    if side_up == "YES":
        return round(float(size_usd) * (1.0 / p - 1.0), 4)
    return round(float(size_usd) * (p / (1.0 - p)), 4)


def evidence_strength(n: int) -> Dict[str, Any]:
    if n < 10:
        label = "anecdotal_only"
    elif n < 30:
        label = "preliminary"
    elif n < 100:
        label = "directional"
    elif n < 300:
        label = "moderate_evidence"
    else:
        label = "stronger_evidence"
    return {
        "classification": label,
        "resolved_forecasts": n,
        "minimum_for_any_edge_claim": 30,
        "edge_claim_permitted": n >= 30,
    }


def _bootstrap_mean_interval(values: Sequence[float]) -> Optional[Dict[str, float]]:
    if len(values) < 10:
        return None
    rng = random.Random(BOOTSTRAP_SEED)
    estimates = sorted(
        mean(rng.choice(values) for _ in values)
        for _ in range(BOOTSTRAP_SAMPLES)
    )
    lower = estimates[int(0.025 * (BOOTSTRAP_SAMPLES - 1))]
    upper = estimates[int(0.975 * (BOOTSTRAP_SAMPLES - 1))]
    return {"lower": lower, "upper": upper, "method": "deterministic_bootstrap_95pct"}


def _calibration_buckets(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    if len(records) < 10:
        return {
            "available": False,
            "reason": "requires_at_least_10_resolved_forecasts",
            "buckets": [],
        }
    buckets: List[Dict[str, Any]] = []
    for low_index in range(10):
        low = low_index / 10.0
        high = (low_index + 1) / 10.0
        selected = [
            row for row in records
            if row["model_probability"] >= low
            and (row["model_probability"] < high or (low_index == 9 and row["model_probability"] <= high))
        ]
        if selected:
            buckets.append({
                "lower": low,
                "upper": high,
                "count": len(selected),
                "mean_probability": mean(row["model_probability"] for row in selected),
                "observed_yes_rate": mean(row["outcome_value"] for row in selected),
            })
    return {"available": True, "reason": None, "buckets": buckets}


def _record_from_row(row: sqlite3.Row) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    status = str(row["status"] or "").upper()
    outcome = str(row["resolved_outcome"] or "").upper()
    exclusion = None
    if status == "VOID":
        exclusion = "status_void"
    elif status == "OPEN":
        exclusion = "status_open_unresolved"
    elif status == "PENDING":
        exclusion = "status_pending_unresolved"
    elif status != "CLOSED":
        exclusion = f"status_{status.lower() or 'missing'}_not_closed"
    elif outcome not in ("YES", "NO"):
        exclusion = "closed_without_binary_outcome"

    model_probability = _probability(row["p_yes"])
    if model_probability is None:
        model_probability = _probability(row["consensus_p_yes"])
    if exclusion is None and model_probability is None:
        exclusion = "missing_or_invalid_model_probability"
    if exclusion is not None:
        return None, {
            "id": int(row["id"]),
            "market_id": str(row["market_id"]),
            "status": status,
            "resolved_outcome": row["resolved_outcome"],
            "exclusion_reason": exclusion,
        }

    base_notes, resolution = _notes_parts(row["notes"])
    market_probability = _probability(base_notes.get("p_yes_market"))
    category = str(base_notes.get("category") or "unknown")
    llm = base_notes.get("llm")
    model = str(llm.get("model") or "unknown") if isinstance(llm, dict) else "unknown"
    if model == "unknown" and base_notes.get("model"):
        model = str(base_notes["model"])
    outcome_value = 1 if outcome == "YES" else 0
    signed_edge = _safe_float(base_notes.get("edge_vs_market"))
    if signed_edge is None and market_probability is not None:
        signed_edge = float(model_probability) - market_probability

    stored_profit = _safe_float(resolution.get("profit_usd"))
    if stored_profit is not None:
        profit = stored_profit
        profit_source = "resolver_metadata"
    else:
        profit = theoretical_profit_usd(
            str(row["side"]),
            float(row["size_usd"]),
            outcome,
            market_probability,
        )
        profit_source = (
            "derived_resolver_formula"
            if market_probability is not None
            else "derived_resolver_even_money_fallback"
        )

    calculated_brier = brier_score(model_probability, outcome_value)
    stored_brier = _safe_float(row["brier"])
    resolved_at = resolution.get("resolved_at_utc")
    month = str(resolved_at)[:7] if resolved_at and len(str(resolved_at)) >= 7 else "unknown"
    correct_side = (str(row["side"]).upper() == outcome)
    record = {
        "id": int(row["id"]),
        "run_id": str(row["run_id"]),
        "market_id": str(row["market_id"]),
        "question": str(row["question"]),
        "entry_ts_utc": str(row["ts_utc"]),
        "resolved_at_utc": resolved_at,
        "month": month,
        "venue": str(row["venue"]),
        "model": model,
        "category": category,
        "reason": str(row["reason"] or "unknown"),
        "side": str(row["side"]).upper(),
        "outcome": outcome,
        "outcome_value": outcome_value,
        "model_probability": model_probability,
        "market_probability": market_probability,
        "model_brier": calculated_brier,
        "stored_model_brier": stored_brier,
        "stored_brier_matches": stored_brier is None or math.isclose(stored_brier, calculated_brier, rel_tol=0.0, abs_tol=1e-12),
        "market_brier": brier_score(market_probability, outcome_value) if market_probability is not None else None,
        "model_log_loss": log_loss(model_probability, outcome_value),
        "market_log_loss": log_loss(market_probability, outcome_value) if market_probability is not None else None,
        "absolute_calibration_error": abs(model_probability - outcome_value),
        "signed_edge": signed_edge,
        "absolute_edge": abs(signed_edge) if signed_edge is not None else None,
        "size_usd": float(row["size_usd"]),
        "profit_usd": profit,
        "profit_source": profit_source,
        "correct_side": correct_side,
        "lookup_source": resolution.get("lookup_source"),
    }
    return record, None


def _mean_available(records: Iterable[Dict[str, Any]], key: str) -> Optional[float]:
    values = [float(row[key]) for row in records if row.get(key) is not None]
    return mean(values) if values else None


def summarize_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    model_briers = [float(row["model_brier"]) for row in records]
    matched = [row for row in records if row.get("market_brier") is not None]
    matched_model_brier = _mean_available(matched, "model_brier")
    market_brier = _mean_available(matched, "market_brier")
    profits = [float(row["profit_usd"]) for row in records if row.get("profit_usd") is not None]
    hit_values = [1.0 if row["correct_side"] else 0.0 for row in records]
    return {
        "resolved_sample_count": len(records),
        "market_matched_sample_count": len(matched),
        "model_brier": mean(model_briers) if model_briers else None,
        "model_brier_on_market_matched_subset": matched_model_brier,
        "market_brier": market_brier,
        "brier_skill_score_vs_market": brier_skill_score(matched_model_brier, market_brier),
        "model_absolute_calibration_error": _mean_available(records, "absolute_calibration_error"),
        "model_log_loss": _mean_available(records, "model_log_loss"),
        "market_log_loss": _mean_available(matched, "market_log_loss"),
        "total_realized_pnl_usd": sum(profits) if profits else None,
        "average_pnl_usd": mean(profits) if profits else None,
        "pnl_sample_count": len(profits),
        "hit_rate": mean(hit_values) if hit_values else None,
        "average_signed_edge": _mean_available(records, "signed_edge"),
        "average_absolute_edge": _mean_available(records, "absolute_edge"),
        "model_brier_ci_95": _bootstrap_mean_interval(model_briers),
    }


def _group_results(records: Sequence[Dict[str, Any]], field: str) -> List[Dict[str, Any]]:
    labels = sorted({str(row.get(field) or "unknown") for row in records})
    results: List[Dict[str, Any]] = []
    for label in labels:
        selected = [
            row for row in records
            if str(row.get(field) or "unknown") == label
        ]
        results.append({
            "label": label,
            "evidence_strength": evidence_strength(len(selected)),
            **summarize_records(selected),
        })
    return results


def _database_inventory(conn: sqlite3.Connection) -> Dict[str, Any]:
    tables = [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )]
    row_counts = {table: conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in tables}
    columns = {
        table: [row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')]
        for table in tables
    }
    status_counts = dict(conn.execute(
        "SELECT status,COUNT(*) FROM paper_trades GROUP BY status ORDER BY status"
    ))
    reason_counts = dict(conn.execute(
        "SELECT reason,COUNT(*) FROM paper_trades GROUP BY reason ORDER BY reason"
    ))
    runs_outcome_counts = (
        dict(conn.execute("SELECT outcome,COUNT(*) FROM runs GROUP BY outcome ORDER BY outcome"))
        if "runs" in tables
        else {}
    )
    return {
        "tables": tables,
        "row_counts": row_counts,
        "columns": columns,
        "forecasts_table_present": "forecasts" in tables,
        "scoring_table_present": "scoring" in tables,
        "resolution_table_present": "resolutions" in tables,
        "diagnostics_table_present": "diagnostics" in tables,
        "paper_trade_status_counts": status_counts,
        "paper_trade_reason_counts": reason_counts,
        "runs_outcome_counts": runs_outcome_counts,
        "runs_calibration_eligible_count": sum(
            count for outcome, count in runs_outcome_counts.items()
            if str(outcome).upper() in ("YES", "NO")
        ),
    }


def _paper_trade_field_inventory(rows: Sequence[sqlite3.Row]) -> Dict[str, Any]:
    models: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    notes_object_counts: Counter[str] = Counter()
    entry_market_probability_count = 0
    valid_model_probability_count = 0
    binary_outcome_count = 0
    stored_brier_count = 0
    resolver_profit_count = 0
    resolution_timestamp_count = 0
    for row in rows:
        objects = _json_objects(row["notes"])
        notes_object_counts[str(len(objects))] += 1
        base, resolution = _notes_parts(row["notes"])
        if _probability(base.get("p_yes_market")) is not None:
            entry_market_probability_count += 1
        if _probability(row["p_yes"]) is not None or _probability(row["consensus_p_yes"]) is not None:
            valid_model_probability_count += 1
        if str(row["resolved_outcome"] or "").upper() in ("YES", "NO"):
            binary_outcome_count += 1
        if _safe_float(row["brier"]) is not None:
            stored_brier_count += 1
        if _safe_float(resolution.get("profit_usd")) is not None:
            resolver_profit_count += 1
        if resolution.get("resolved_at_utc"):
            resolution_timestamp_count += 1
        llm = base.get("llm")
        model = llm.get("model") if isinstance(llm, dict) else base.get("model")
        models[str(model or "unknown")] += 1
        categories[str(base.get("category") or "unknown")] += 1
    return {
        "notes_json_object_count_distribution": dict(sorted(notes_object_counts.items())),
        "model_identifiers": dict(sorted(models.items())),
        "categories": dict(sorted(categories.items())),
        "valid_model_probability_count": valid_model_probability_count,
        "entry_market_probability_count": entry_market_probability_count,
        "binary_resolved_outcome_count": binary_outcome_count,
        "stored_brier_count": stored_brier_count,
        "resolver_profit_count": resolver_profit_count,
        "resolution_timestamp_count": resolution_timestamp_count,
    }


def _paper_trade_fingerprint(rows: Sequence[sqlite3.Row]) -> str:
    return hashlib.sha256(
        json.dumps([tuple(row) for row in rows], default=str).encode()
    ).hexdigest()


def analyze_database(db_path: Path) -> Dict[str, Any]:
    resolved = db_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Database not found: {resolved}")
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    try:
        inventory = _database_inventory(conn)
        rows = conn.execute("SELECT * FROM paper_trades ORDER BY id").fetchall()
    finally:
        conn.close()

    included: List[Dict[str, Any]] = []
    excluded: List[Dict[str, Any]] = []
    for row in rows:
        record, exclusion = _record_from_row(row)
        if record is not None:
            included.append(record)
        if exclusion is not None:
            excluded.append(exclusion)

    inventory["paper_trade_fields"] = _paper_trade_field_inventory(rows)

    overall = summarize_records(included)
    warnings: List[str] = []
    if len(included) < 10:
        warnings.append(
            f"Only {len(included)} resolved forecasts are available; results are anecdotal only and cannot establish edge."
        )
    mismatched_brier_ids = [row["id"] for row in included if not row["stored_brier_matches"]]
    if mismatched_brier_ids:
        warnings.append(f"Stored Brier differs from recalculation for trade IDs: {mismatched_brier_ids}")
    if any(row["market_probability"] is None for row in included):
        warnings.append("Some included forecasts lack an entry market probability and are excluded from market-relative metrics.")

    valid_for = {
        "calibration_scoring_ids": [row["id"] for row in included],
        "pnl_scoring_ids": [row["id"] for row in included if row["profit_usd"] is not None],
        "model_comparison_ids": [row["id"] for row in included if row["model"] != "unknown"],
        "category_analysis_ids": [row["id"] for row in included if row["category"] != "unknown"],
        "reason_analysis_ids": [row["id"] for row in included if row["reason"] != "unknown"],
        "market_comparison_ids": [row["id"] for row in included if row["market_probability"] is not None],
    }
    return {
        "method_version": METHOD_VERSION,
        "source_database": str(db_path),
        "source_paper_trades_sha256": _paper_trade_fingerprint(rows),
        "inventory": inventory,
        "valid_for": valid_for,
        "evidence_strength": evidence_strength(len(included)),
        "warnings": warnings,
        "pnl_assumptions": {
            "classification": "theoretical_gross_paper_pnl",
            "fees_included": False,
            "slippage_included": False,
            "spread_crossing_modeled": False,
            "entry_probability_source": "notes.p_yes_market",
        },
        "overall": overall,
        "calibration_buckets": _calibration_buckets(included),
        "by_model": _group_results(included, "model"),
        "by_category": _group_results(included, "category"),
        "by_side": _group_results(included, "side"),
        "by_reason": _group_results(included, "reason"),
        "by_month": _group_results(included, "month"),
        "included_trades": included,
        "excluded_trades": excluded,
    }


def _write_csv(path: Path, records: Sequence[Dict[str, Any]]) -> None:
    fields = [
        "id", "market_id", "question", "entry_ts_utc", "resolved_at_utc", "month",
        "venue", "model", "category", "reason", "side", "outcome",
        "model_probability", "market_probability", "model_brier", "market_brier",
        "model_log_loss", "market_log_loss", "signed_edge", "absolute_edge",
        "size_usd", "profit_usd", "profit_source", "correct_side", "lookup_source",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=fields,
            extrasaction="ignore",
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(records)


def _fmt(value: Any, digits: int = 6) -> str:
    if value is None:
        return "unavailable"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _write_markdown(path: Path, report: Dict[str, Any]) -> None:
    overall = report["overall"]
    inventory = report["inventory"]
    evidence = report["evidence_strength"]
    lines = [
        "# Phase 2 Calibration Baseline",
        "",
        f"- Evidence strength: **{evidence['classification']}**",
        f"- Resolved forecasts: `{overall['resolved_sample_count']}`",
        f"- Included trade IDs: `{report['valid_for']['calibration_scoring_ids']}`",
        f"- Excluded trades: `{len(report['excluded_trades'])}`",
        f"- Model Brier: `{_fmt(overall['model_brier'])}`",
        f"- Market Brier: `{_fmt(overall['market_brier'])}`",
        f"- Brier skill vs market: `{_fmt(overall['brier_skill_score_vs_market'])}`",
        f"- Model log loss: `{_fmt(overall['model_log_loss'])}`",
        f"- Market log loss: `{_fmt(overall['market_log_loss'])}`",
        f"- Total theoretical gross P&L: `${_fmt(overall['total_realized_pnl_usd'], 4)}`",
        f"- Average P&L/trade: `${_fmt(overall['average_pnl_usd'], 4)}`",
        f"- Hit rate: `{_fmt(overall['hit_rate'])}`",
        f"- Average signed edge: `{_fmt(overall['average_signed_edge'])}`",
        f"- Average absolute edge: `{_fmt(overall['average_absolute_edge'])}`",
        "",
        "> Two resolved trades are anecdotal only and are insufficient to establish forecasting or trading edge.",
        "",
        "## Inventory",
        "",
        f"- Tables: `{inventory['tables']}`",
        f"- Row counts: `{inventory['row_counts']}`",
        f"- Paper trade statuses: `{inventory['paper_trade_status_counts']}`",
        f"- Forecasts table present: `{str(inventory['forecasts_table_present']).lower()}`",
        "",
        "## Included Trades",
        "",
        "| ID | Model | Category | Side | Outcome | Model p | Market p | Model Brier | Market Brier | P&L |",
        "|---:|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for row in report["included_trades"]:
        lines.append(
            f"| {row['id']} | {row['model']} | {row['category']} | {row['side']} | {row['outcome']} | "
            f"{_fmt(row['model_probability'], 4)} | {_fmt(row['market_probability'], 4)} | "
            f"{_fmt(row['model_brier'], 4)} | {_fmt(row['market_brier'], 4)} | {_fmt(row['profit_usd'], 4)} |"
        )
    lines += [
        "",
        "## Exclusions",
        "",
        "| ID | Status | Exclusion |",
        "|---:|---|---|",
    ]
    for row in report["excluded_trades"]:
        lines.append(f"| {row['id']} | {row['status']} | {row['exclusion_reason']} |")
    lines += [
        "",
        "## Interpretation",
        "",
        "P&L is theoretical gross paper P&L. Fees, slippage, spread crossing, latency, and fill risk are not modeled. Calibration buckets and confidence intervals are withheld because the sample has fewer than 10 resolved forecasts.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_reports(report: Dict[str, Any], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "phase2_calibration_baseline.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _write_markdown(output_dir / "phase2_calibration_baseline.md", report)
    _write_csv(output_dir / "phase2_calibration_trades.csv", report["included_trades"])


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Phase 2 paper-trade calibration baseline")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    report = analyze_database(args.db)
    write_reports(report, args.output_dir)
    overall = report["overall"]
    print(
        "CALIBRATION "
        f"resolved={overall['resolved_sample_count']} "
        f"model_brier={_fmt(overall['model_brier'])} "
        f"market_brier={_fmt(overall['market_brier'])} "
        f"skill={_fmt(overall['brier_skill_score_vs_market'])} "
        f"pnl_usd={_fmt(overall['total_realized_pnl_usd'], 4)} "
        f"evidence={report['evidence_strength']['classification']}"
    )
    for warning in report["warnings"]:
        print(f"WARNING: {warning}")
    print(f"WROTE {args.output_dir / 'phase2_calibration_baseline.json'}")
    print(f"WROTE {args.output_dir / 'phase2_calibration_baseline.md'}")
    print(f"WROTE {args.output_dir / 'phase2_calibration_trades.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
