from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
UPGRADE = ROOT / "migrations" / "006_revenue_poc_v1.sql"
DOWNGRADE = ROOT / "migrations" / "006_revenue_poc_v1_down.sql"
UPGRADE_V11 = ROOT / "migrations" / "007_revenue_poc_v1_1.sql"
DOWNGRADE_V11 = ROOT / "migrations" / "007_revenue_poc_v1_1_down.sql"
UPGRADE_DISCOVERY_METADATA = ROOT / "migrations" / "008_discovery_source_metadata.sql"
DOWNGRADE_DISCOVERY_METADATA = ROOT / "migrations" / "008_discovery_source_metadata_down.sql"


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(UPGRADE.read_text(encoding="utf-8"))
    # The v1.1 migration is intentionally idempotent for copied production
    # databases.  Existing v1 databases receive the optimization telemetry;
    # fresh test databases receive both migrations in one call.
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(revenue_poc_api_calls)")
    }
    if "routing_tier" not in columns:
        conn.executescript(UPGRADE_V11.read_text(encoding="utf-8"))
    snapshot_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(revenue_poc_discovery_snapshots)")
    }
    if snapshot_columns and "source_event_category" not in snapshot_columns:
        conn.executescript(UPGRADE_DISCOVERY_METADATA.read_text(encoding="utf-8"))
    conn.commit()


def downgrade_schema(conn: sqlite3.Connection) -> None:
    snapshot_columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(revenue_poc_discovery_snapshots)")
    }
    if "source_event_category" in snapshot_columns:
        conn.executescript(DOWNGRADE_DISCOVERY_METADATA.read_text(encoding="utf-8"))
    conn.executescript(DOWNGRADE_V11.read_text(encoding="utf-8"))
    conn.executescript(DOWNGRADE.read_text(encoding="utf-8"))
    conn.commit()


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def persist_discovery_snapshots(
    conn: sqlite3.Connection,
    *,
    discovery_result: dict[str, Any],
    run_id: str,
    timestamp_utc: str,
    venue: str,
) -> dict[str, Any]:
    """Persist one discovery result and return bounded persistence diagnostics.

    Historical rows are never updated.  The unique ``(run_id, venue,
    market_id)`` constraint remains append-only; ignored rows are counted
    explicitly instead of being mistaken for successful inserts.
    """
    diagnostics: dict[str, Any] = {
        "discovered_rows": len(discovery_result.get("rows") or []),
        "rows_prepared": 0,
        "rows_inserted": 0,
        "rows_ignored": 0,
        "rows_failed": 0,
        "missing_market_ids": 0,
        "source_metadata_rows": 0,
        "reporting_class_rows": 0,
        "persistence_error": None,
        "persistence_error_samples": [],
    }
    table = "revenue_poc_discovery_snapshots"
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if table not in tables:
        diagnostics["persistence_error"] = "missing_table"
        return diagnostics

    columns = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    enriched = "source_event_category" in columns
    prepared: list[tuple[Any, ...]] = []
    for row in discovery_result.get("rows") or []:
        market_id = str(row.get("market_id") or "").strip()
        if not market_id:
            diagnostics["missing_market_ids"] += 1
            continue
        features = row.get("features") if isinstance(row.get("features"), dict) else {}
        source = features.get("source_metadata") if isinstance(features.get("source_metadata"), dict) else {}
        if source and source != {}:
            diagnostics["source_metadata_rows"] += 1
        if features.get("reporting_class"):
            diagnostics["reporting_class_rows"] += 1
        base = (
            run_id, timestamp_utc, venue, market_id, row.get("status", "UNKNOWN"),
            row.get("reason"), int(bool(row.get("fixed_watchlist"))),
            int(bool(row.get("dynamic_shortlist"))), row.get("score"),
            json.dumps(features, sort_keys=True),
        )
        if enriched:
            base += (
                source.get("event_category"), source.get("event_title"),
                json.dumps(source.get("tags", []), sort_keys=True), source.get("series"),
                source.get("source_type"), features.get("policy_market_class"),
                features.get("policy_reason"), features.get("reporting_class"),
                json.dumps(source, sort_keys=True),
            )
        prepared.append(base)

    diagnostics["rows_prepared"] = len(prepared)
    if not prepared:
        return diagnostics

    before = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE run_id=? AND venue=?",
        (run_id, venue),
    ).fetchone()[0]
    if enriched:
        sql = f"""INSERT INTO {table}
            (run_id,timestamp_utc,venue,market_id,status,rejection_reason,
             fixed_watchlist,dynamic_shortlist,deterministic_score,metadata,
             source_event_category,source_event_title,source_tags,source_series,
             source_type,policy_market_class,policy_rejection_reason,
             reporting_class,source_metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,venue,market_id) DO NOTHING"""
    else:
        sql = f"""INSERT INTO {table}
            (run_id,timestamp_utc,venue,market_id,status,rejection_reason,
             fixed_watchlist,dynamic_shortlist,deterministic_score,metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(run_id,venue,market_id) DO NOTHING"""
    try:
        with conn:
            conn.executemany(sql, prepared)
    except sqlite3.Error as exc:
        diagnostics["persistence_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
        for values in prepared:
            try:
                with conn:
                    conn.execute(sql, values)
            except sqlite3.Error as row_exc:
                diagnostics["rows_failed"] += 1
                if len(diagnostics["persistence_error_samples"]) < 3:
                    diagnostics["persistence_error_samples"].append(
                        f"{type(row_exc).__name__}: {str(row_exc)[:240]}"
                    )

    after = conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE run_id=? AND venue=?",
        (run_id, venue),
    ).fetchone()[0]
    diagnostics["rows_inserted"] = max(0, int(after) - int(before))
    diagnostics["rows_ignored"] = max(
        0, diagnostics["rows_prepared"] - diagnostics["rows_inserted"] - diagnostics["rows_failed"]
    )
    return diagnostics
