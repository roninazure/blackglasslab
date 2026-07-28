from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parent.parent
MIGRATION_PATH = ROOT / "migrations" / "005_phase3_3_shadow_forecasts.sql"
DEFAULT_THRESHOLDS = (0.02, 0.03, 0.04, 0.05)


def _utc(value: Optional[datetime] = None) -> datetime:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc)


def _parse_datetime(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def backup_database(
    conn: sqlite3.Connection,
    db_path: str | Path,
    *,
    backup_dir: str | Path = "backups",
    now: Optional[datetime] = None,
) -> Optional[Path]:
    path = Path(db_path)
    if str(path) == ":memory:" or not path.exists() or path.stat().st_size == 0:
        return None
    destination_dir = Path(backup_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    stamp = _utc(now).strftime("%Y%m%dT%H%M%S%fZ")
    destination = destination_dir / f"{path.stem}.pre_phase3_3.{stamp}.sqlite"
    backup_conn = sqlite3.connect(destination)
    try:
        conn.backup(backup_conn)
        backup_conn.commit()
    finally:
        backup_conn.close()
    return destination


def ensure_shadow_schema(
    conn: sqlite3.Connection,
    *,
    db_path: str | Path = ":memory:",
    backup_dir: str | Path = "backups",
    now: Optional[datetime] = None,
) -> Optional[Path]:
    """Create the Phase 3.3 schema, backing up an existing DB first."""
    if _table_exists(conn, "shadow_forecasts") and _table_exists(
        conn, "shadow_threshold_results"
    ):
        return None
    backup_path = backup_database(
        conn, db_path, backup_dir=backup_dir, now=now
    )
    conn.executescript(MIGRATION_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return backup_path


def parse_thresholds(raw: str | Iterable[float] | None) -> tuple[float, ...]:
    if raw is None:
        return DEFAULT_THRESHOLDS
    values: list[float] = []
    source: Iterable[Any] = raw.split(",") if isinstance(raw, str) else raw
    for item in source:
        try:
            value = float(str(item).strip())
        except (TypeError, ValueError):
            continue
        if 0.0 <= value <= 1.0 and value not in values:
            values.append(value)
    return tuple(sorted(values)) or DEFAULT_THRESHOLDS


def threshold_specs(
    thresholds: Sequence[float], production_threshold: float
) -> tuple[tuple[str, float, bool], ...]:
    specs = [
        (f">={int(round(float(value) * 100))}%", float(value), False)
        for value in thresholds
    ]
    specs.append(("current production threshold", float(production_threshold), True))
    return tuple(specs)


def time_to_resolution_days(
    market_end_date: Any, *, timestamp_utc: Any
) -> Optional[float]:
    end = _parse_datetime(market_end_date)
    started = _parse_datetime(timestamp_utc)
    if end is None or started is None:
        return None
    return round(max(0.0, (end - started).total_seconds() / 86400.0), 6)


@dataclass(frozen=True)
class ShadowInsertResult:
    forecast_id: int
    inserted: bool


def insert_shadow_forecast(
    conn: sqlite3.Connection,
    forecast: Mapping[str, Any],
    *,
    thresholds: Sequence[float] = DEFAULT_THRESHOLDS,
    production_threshold: float,
    hypothetical_stake_usd: float = 100.0,
) -> ShadowInsertResult:
    """Insert once; a duplicate never rewrites the original forecast."""
    run_id = str(forecast["run_id"])
    venue = str(forecast.get("venue") or "polymarket")
    market_id = str(forecast.get("market_id") or forecast.get("slug"))
    forecast_key = str(
        forecast.get("forecast_key") or f"{run_id}:{venue}:{market_id}"
    )
    timestamp = str(forecast.get("timestamp_utc") or forecast.get("ts_utc"))
    market_probability = float(forecast["market_probability"])
    model_probability = float(forecast["model_probability"])
    absolute_edge = float(
        forecast.get("absolute_edge", abs(model_probability - market_probability))
    )
    end_date = forecast.get("market_end_date")
    ttr = forecast.get("time_to_resolution_days")
    if ttr is None:
        ttr = time_to_resolution_days(end_date, timestamp_utc=timestamp)

    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO shadow_forecasts (
          forecast_key, run_id, timestamp_utc, venue, market_id, slug, question,
          category, market_probability, model_probability, side, absolute_edge,
          opportunity_score, quality_score, grade, contract_validity,
          opportunity_quality, model_edge, production_decision, rejection_reason,
          temporal_validation, skeptic_result, market_end_date,
          time_to_resolution_days, llm_used, model_name, metadata
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            forecast_key,
            run_id,
            timestamp,
            venue,
            market_id,
            str(forecast.get("slug") or market_id),
            str(forecast.get("question") or market_id),
            str(forecast.get("category") or "unknown"),
            market_probability,
            model_probability,
            str(forecast.get("side") or ("YES" if model_probability >= market_probability else "NO")),
            absolute_edge,
            forecast.get("opportunity_score"),
            forecast.get("quality_score"),
            forecast.get("grade"),
            str(forecast.get("contract_validity") or "valid"),
            str(forecast.get("opportunity_quality") or "qualified"),
            str(forecast.get("model_edge") or "evaluated"),
            str(forecast.get("production_decision") or "rejected"),
            forecast.get("rejection_reason"),
            forecast.get("temporal_validation"),
            json.dumps(forecast.get("skeptic_result"), sort_keys=True)
            if forecast.get("skeptic_result") is not None
            else None,
            end_date,
            ttr,
            1 if forecast.get("llm_used") else 0,
            forecast.get("model_name"),
            json.dumps(forecast.get("metadata") or {}, sort_keys=True),
        ),
    )
    inserted = cursor.rowcount == 1
    row = conn.execute(
        "SELECT id FROM shadow_forecasts WHERE forecast_key=?", (forecast_key,)
    ).fetchone()
    if row is None:
        raise RuntimeError("shadow forecast insert could not be verified")
    forecast_id = int(row[0])
    if inserted:
        for label, threshold, is_production in threshold_specs(
            thresholds, production_threshold
        ):
            conn.execute(
                """
                INSERT OR IGNORE INTO shadow_threshold_results (
                  shadow_forecast_id, bucket_label, threshold,
                  is_production_threshold, qualifies, hypothetical_stake_usd
                ) VALUES (?,?,?,?,?,?)
                """,
                (
                    forecast_id,
                    label,
                    threshold,
                    1 if is_production else 0,
                    1 if absolute_edge >= threshold else 0,
                    max(0.01, float(hypothetical_stake_usd)),
                ),
            )
    conn.commit()
    return ShadowInsertResult(forecast_id=forecast_id, inserted=inserted)


def brier_score(model_probability: float, outcome: str) -> float:
    actual = 1.0 if str(outcome).upper() == "YES" else 0.0
    return (float(model_probability) - actual) ** 2


def hypothetical_profit(
    *, side: str, stake_usd: float, outcome: str, market_probability: float
) -> float:
    side = side.upper()
    outcome = outcome.upper()
    correct = side == outcome
    if not correct:
        return -float(stake_usd)
    probability = max(0.001, min(0.999, float(market_probability)))
    if side == "YES":
        return round(float(stake_usd) * (1.0 / probability - 1.0), 4)
    return round(float(stake_usd) * (probability / (1.0 - probability)), 4)


def resolve_shadow_forecast(
    conn: sqlite3.Connection,
    forecast_id: int,
    outcome: str,
    *,
    resolved_at_utc: Optional[str] = None,
) -> bool:
    outcome = str(outcome).upper()
    if outcome not in {"YES", "NO"}:
        raise ValueError("outcome must be YES or NO")
    row = conn.execute(
        """
        SELECT timestamp_utc, market_probability, model_probability, side, status
        FROM shadow_forecasts WHERE id=?
        """,
        (int(forecast_id),),
    ).fetchone()
    if row is None:
        return False
    if row[4] == "RESOLVED":
        return False
    resolved_at = resolved_at_utc or _utc().isoformat()
    start = _parse_datetime(row[0])
    end = _parse_datetime(resolved_at)
    holding_days = (
        round(max(0.0, (end - start).total_seconds() / 86400.0), 6)
        if start is not None and end is not None
        else None
    )
    won = 1 if str(row[3]).upper() == outcome else 0
    score = brier_score(float(row[2]), outcome)
    stake_row = conn.execute(
        "SELECT COALESCE(MAX(hypothetical_stake_usd), 100.0) FROM shadow_threshold_results WHERE shadow_forecast_id=?",
        (int(forecast_id),),
    ).fetchone()
    stake = float(stake_row[0] if stake_row else 100.0)
    pnl = hypothetical_profit(
        side=str(row[3]),
        stake_usd=stake,
        outcome=outcome,
        market_probability=float(row[1]),
    )
    conn.execute(
        """
        UPDATE shadow_forecasts
        SET status='RESOLVED', eventual_outcome=?, resolved_at_utc=?,
            brier_score=?, hypothetical_win=?, hypothetical_pnl=?, roi=?,
            holding_period_days=?
        WHERE id=? AND status='OPEN'
        """,
        (outcome, resolved_at, score, won, pnl, pnl / stake, holding_days, int(forecast_id)),
    )
    buckets = conn.execute(
        """
        SELECT id, hypothetical_stake_usd FROM shadow_threshold_results
        WHERE shadow_forecast_id=? AND qualifies=1
        """,
        (int(forecast_id),),
    ).fetchall()
    for bucket_id, bucket_stake in buckets:
        bucket_pnl = hypothetical_profit(
            side=str(row[3]),
            stake_usd=float(bucket_stake),
            outcome=outcome,
            market_probability=float(row[1]),
        )
        conn.execute(
            """
            UPDATE shadow_threshold_results
            SET hypothetical_win=?, hypothetical_pnl=?, roi=?, holding_period_days=?
            WHERE id=?
            """,
            (won, bucket_pnl, bucket_pnl / float(bucket_stake), holding_days, bucket_id),
        )
    conn.commit()
    return True


def _calibration_summary(rows: Sequence[sqlite3.Row | tuple[Any, ...]]) -> list[dict[str, Any]]:
    bands = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.000001))
    result: list[dict[str, Any]] = []
    for low, high in bands:
        selected = [row for row in rows if low <= float(row[0]) < high]
        if not selected:
            continue
        result.append(
            {
                "band": f"{low:.1f}-{min(high, 1.0):.1f}",
                "resolved": len(selected),
                "mean_probability": round(sum(float(row[0]) for row in selected) / len(selected), 6),
                "actual_rate": round(sum(1 if row[1] == "YES" else 0 for row in selected) / len(selected), 6),
                "brier": round(sum(float(row[2]) for row in selected) / len(selected), 6),
            }
        )
    return result


def shadow_summary(
    conn: sqlite3.Connection, *, now: Optional[datetime] = None
) -> dict[str, Any]:
    current = _utc(now)
    today = current.date().isoformat()
    total = int(conn.execute("SELECT COUNT(*) FROM shadow_forecasts").fetchone()[0])
    today_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM shadow_forecasts WHERE substr(timestamp_utc,1,10)=?",
            (today,),
        ).fetchone()[0]
    )
    resolved = int(
        conn.execute(
            "SELECT COUNT(*) FROM shadow_forecasts WHERE status='RESOLVED'"
        ).fetchone()[0]
    )
    latest = conn.execute(
        "SELECT run_id, timestamp_utc FROM shadow_forecasts ORDER BY id DESC LIMIT 1"
    ).fetchone()
    evaluations = 0
    llm_calls = 0
    if latest:
        evaluations, llm_calls = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(llm_used),0) FROM shadow_forecasts WHERE run_id=?",
            (latest[0],),
        ).fetchone()
    bucket_rows = conn.execute(
        """
        SELECT r.bucket_label, r.threshold, r.is_production_threshold,
               SUM(r.qualifies),
               SUM(CASE WHEN r.qualifies=1 AND f.status='RESOLVED' THEN 1 ELSE 0 END),
               SUM(CASE WHEN r.qualifies=1 AND r.hypothetical_win=1 THEN 1 ELSE 0 END),
               SUM(CASE WHEN r.qualifies=1 AND r.hypothetical_win=0 THEN 1 ELSE 0 END),
               AVG(CASE WHEN r.qualifies=1 AND f.status='RESOLVED' THEN f.brier_score END),
               SUM(CASE WHEN r.qualifies=1 THEN r.hypothetical_pnl ELSE 0 END),
               SUM(CASE WHEN r.qualifies=1 AND f.status='RESOLVED' THEN r.hypothetical_stake_usd ELSE 0 END),
               AVG(CASE WHEN r.qualifies=1 THEN r.holding_period_days END)
        FROM shadow_threshold_results r
        JOIN shadow_forecasts f ON f.id=r.shadow_forecast_id
        GROUP BY r.bucket_label, r.threshold, r.is_production_threshold
        ORDER BY r.is_production_threshold, r.threshold
        """
    ).fetchall()
    buckets: list[dict[str, Any]] = []
    for row in bucket_rows:
        display_label = (
            f"current production threshold (>={int(round(float(row[1]) * 100))}%)"
            if bool(row[2])
            else row[0]
        )
        calibration_rows = conn.execute(
            """
            SELECT f.model_probability, f.eventual_outcome, f.brier_score
            FROM shadow_threshold_results r
            JOIN shadow_forecasts f ON f.id=r.shadow_forecast_id
            WHERE r.bucket_label=? AND r.threshold=?
              AND r.is_production_threshold=?
              AND r.qualifies=1 AND f.status='RESOLVED'
            """,
            (row[0], row[1], row[2]),
        ).fetchall()
        wagered = float(row[9] or 0.0)
        pnl = float(row[8] or 0.0)
        buckets.append(
            {
                "label": display_label,
                "threshold": float(row[1]),
                "production": bool(row[2]),
                "forecasts": int(row[3] or 0),
                "resolved": int(row[4] or 0),
                "wins": int(row[5] or 0),
                "losses": int(row[6] or 0),
                "brier": round(float(row[7]), 6) if row[7] is not None else None,
                "pnl": round(pnl, 4),
                "roi": round(pnl / wagered, 6) if wagered else None,
                "average_holding_period_days": round(float(row[10]), 6) if row[10] is not None else None,
                "calibration_bands": _calibration_summary(calibration_rows),
            }
        )
    eligible_best = [bucket for bucket in buckets if bucket["resolved"] and bucket["roi"] is not None]
    best = max(eligible_best, key=lambda bucket: bucket["roi"], default=None)
    horizons = {"<=7d": 0, "8-30d": 0, "31-90d": 0, "91-180d": 0, ">180d": 0, "unknown": 0}
    for (days,) in conn.execute(
        "SELECT time_to_resolution_days FROM shadow_forecasts"
    ).fetchall():
        if days is None:
            horizons["unknown"] += 1
        elif days <= 7:
            horizons["<=7d"] += 1
        elif days <= 30:
            horizons["8-30d"] += 1
        elif days <= 90:
            horizons["31-90d"] += 1
        elif days <= 180:
            horizons["91-180d"] += 1
        else:
            horizons[">180d"] += 1
    latest_dt = _parse_datetime(latest[1]) if latest else None
    freshness = (
        round(max(0.0, (current - latest_dt).total_seconds() / 60.0), 2)
        if latest_dt is not None
        else None
    )
    return {
        "forecasts_today": today_count,
        "forecasts_total": total,
        "resolved_forecasts": resolved,
        "latest_run_id": latest[0] if latest else None,
        "evaluations_per_cycle": int(evaluations),
        "llm_calls_per_cycle": int(llm_calls),
        "threshold_buckets": buckets,
        "best_performing_threshold": best["label"] if best else None,
        "time_to_resolution_distribution": horizons,
        "last_forecast_at_utc": latest[1] if latest else None,
        "data_freshness_minutes": freshness,
    }
