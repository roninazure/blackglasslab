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
from revenue_poc.reporting import (
    alpha_attribution_report,
    funnel_analysis,
    portfolio_dashboard,
    write_report,
)
from revenue_poc.repository import apply_schema, downgrade_schema
from revenue_poc.service import RevenuePOCService
from revenue_poc.velocity import velocity_coverage_report, velocity_shadow_report
from revenue_poc.venue import quote_from_market_and_book, resolved_outcome, yes_token_id
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
    parser.add_argument("--velocity-shadow", action="store_true")
    parser.add_argument("--velocity-coverage", action="store_true")
    parser.add_argument("--alpha-attribution", action="store_true")
    parser.add_argument("--refresh-marks", action="store_true")
    parser.add_argument("--resolve-shadow", action="store_true")
    parser.add_argument("--lifecycle-fixture", type=Path)
    parser.add_argument("--output-dir", type=Path, default=RUNTIME_PATHS.report_dir)
    parser.add_argument("--backup-dir", type=Path, default=RUNTIME_PATHS.backup_dir)
    args = parser.parse_args()
    if args.downgrade and (args.migrate or args.ingest_shadow):
        parser.error("downgrade cannot be combined with migration or ingestion")
    if args.velocity_shadow or args.velocity_coverage or args.alpha_attribution:
        conn = sqlite3.connect(f"file:{args.db.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
    else:
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
        service = RevenuePOCService(conn, RevenueConfig.from_env())
        if args.lifecycle_fixture:
            payload = json.loads(args.lifecycle_fixture.read_text(encoding="utf-8"))
            marked = sum(
                1
                for item in payload.get("marks", [])
                if service.mark_position(int(item["position_id"]), item)
            )
            resolved = sum(
                1
                for item in payload.get("resolutions", [])
                if service.resolve_position(
                    int(item["position_id"]),
                    str(item["outcome"]),
                    item.get("resolved_at_utc"),
                    resolution_fee_usd=float(item.get("resolution_fee_usd") or 0),
                    resolution_slippage_usd=float(item.get("resolution_slippage_usd") or 0),
                )
            )
            print(json.dumps({"fixture_lifecycle": {"marked": marked, "resolved": resolved}}))
        if args.refresh_marks:
            from adapters.polymarket_adapter import PolymarketAdapter

            adapter = PolymarketAdapter()
            marked = resolved = failed = 0
            failures: list[dict[str, object]] = []
            rows = conn.execute(
                "SELECT id,market_id,category FROM revenue_poc_positions WHERE status='OPEN'"
            ).fetchall()
            for position_id, market_id, category in rows:
                try:
                    market = adapter.get_market(str(market_id))
                    outcome = resolved_outcome(market)
                    if outcome:
                        resolved += int(service.resolve_position(int(position_id), outcome))
                        continue
                    book = adapter.get_order_book(yes_token_id(market))
                    marked += int(
                        service.mark_position(
                            int(position_id),
                            quote_from_market_and_book(market, book, category=str(category)),
                        )
                    )
                except Exception as exc:
                    failed += 1
                    failures.append(
                        {
                            "position_id": int(position_id),
                            "market_id": str(market_id),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
            print(
                json.dumps(
                    {
                        "venue_lifecycle": {
                            "marked": marked,
                            "resolved": resolved,
                            "failed": failed,
                            "failures": failures,
                        }
                    }
                )
            )
        if args.resolve_shadow:
            print(json.dumps({"shadow_resolved": service.resolve_from_shadow()}))
        if args.dashboard:
            dashboard = portfolio_dashboard(conn)
            write_report(args.output_dir / "revenue_poc_dashboard.json", dashboard)
            print(json.dumps({"dashboard": dashboard}, sort_keys=True))
        if args.analysis:
            analysis = funnel_analysis(conn, RUNTIME_PATHS.signals_dir / "infer_pipeline_report.json")
            write_report(args.output_dir / "revenue_poc_funnel_analysis.json", analysis)
            print(json.dumps({"analysis": analysis}, sort_keys=True))
        if args.velocity_shadow:
            report = velocity_shadow_report(conn)
            write_report(args.output_dir / "revenue_velocity_shadow_v1.json", report)
            print(json.dumps({"velocity_shadow": report}, sort_keys=True))
        if args.velocity_coverage:
            report = velocity_coverage_report(conn)
            write_report(args.output_dir / "revenue_velocity_evaluation_coverage_v1.json", report)
            print(json.dumps({"velocity_coverage": report}, sort_keys=True))
        if args.alpha_attribution:
            report = alpha_attribution_report(conn)
            write_report(args.output_dir / "revenue_alpha_attribution_v1.json", report)
            print(json.dumps({"alpha_attribution": report}, sort_keys=True))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
