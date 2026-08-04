#!/usr/bin/env python3
"""Operate the independent, strictly paper-only Revenue POC ledger."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from revenue_poc.config import RevenueConfig
from revenue_poc.reporting import funnel_analysis, portfolio_dashboard, write_report
from revenue_poc.repository import apply_schema, downgrade_schema
from revenue_poc.service import RevenuePOCService
from swarm_edge_runtime import RUNTIME_PATHS


def _backup_if_schema_change(conn: sqlite3.Connection, db_path: Path, backup_dir: Path) -> Path | None:
    if not db_path.exists() or str(db_path) == ":memory:":
        return None
    revenue_exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='revenue_poc_accounts'"
    ).fetchone()
    if revenue_exists:
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = backup_dir / f"{db_path.stem}.pre_revenue_poc_v1.{stamp}.sqlite"
    backup_conn = sqlite3.connect(destination)
    try:
        conn.backup(backup_conn)
        backup_conn.commit()
    finally:
        backup_conn.close()
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=RUNTIME_PATHS.db_path)
    parser.add_argument("--migrate", action="store_true")
    parser.add_argument("--downgrade", action="store_true")
    parser.add_argument("--ingest-shadow", action="store_true")
    parser.add_argument("--dashboard", action="store_true")
    parser.add_argument("--analysis", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=RUNTIME_PATHS.report_dir)
    parser.add_argument("--backup-dir", type=Path, default=RUNTIME_PATHS.backup_dir)
    args = parser.parse_args()
    if args.downgrade and (args.migrate or args.ingest_shadow):
        parser.error("downgrade cannot be combined with migration or ingestion")
    conn = sqlite3.connect(args.db)
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        if args.migrate or args.ingest_shadow:
            backup = _backup_if_schema_change(conn, args.db, args.backup_dir)
            if backup:
                print(json.dumps({"pre_migration_backup": str(backup)}))
        if args.downgrade:
            downgrade_schema(conn)
            print(json.dumps({"downgraded": str(args.db)}))
            return 0
        if args.migrate:
            apply_schema(conn)
        if args.ingest_shadow:
            result = RevenuePOCService(conn, RevenueConfig.from_env()).ingest_shadow_forecasts()
            print(json.dumps({"ingest": result}, sort_keys=True))
        if args.dashboard:
            dashboard = portfolio_dashboard(conn)
            write_report(args.output_dir / "revenue_poc_dashboard.json", dashboard)
            print(json.dumps({"dashboard": dashboard}, sort_keys=True))
        if args.analysis:
            analysis = funnel_analysis(conn, RUNTIME_PATHS.signals_dir / "infer_pipeline_report.json")
            write_report(args.output_dir / "revenue_poc_funnel_analysis.json", analysis)
            print(json.dumps({"analysis": analysis}, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
