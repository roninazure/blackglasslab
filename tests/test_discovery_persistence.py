from __future__ import annotations

import json
import sqlite3
import unittest

from reporting.discovery_breakdown import build_discovery_breakdown
from revenue_poc.repository import apply_schema, canonical_json, persist_discovery_snapshots


def _database() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE shadow_forecasts (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL)"
    )
    apply_schema(conn)
    return conn


def _result() -> dict[str, object]:
    return {
        "rows": [
            {
                "market_id": "sports-1",
                "status": "REJECTED",
                "reason": "banned_market_class",
                "fixed_watchlist": False,
                "dynamic_shortlist": True,
                "score": 82.5,
                "features": {
                    "reporting_class": "sports",
                    "policy_market_class": "sports",
                    "policy_reason": None,
                    "source_metadata": {
                        "event_category": "Sports",
                        "event_title": "Final",
                        "tags": [{"label": "soccer"}],
                        "series": ["league-series", {"season": 2026, "stage": "final"}],
                        "source_type": "sports",
                    },
                },
            },
            {
                "market_id": "generic-1",
                "status": "REJECTED",
                "reason": "banned_market_class",
                "features": {
                    "reporting_class": "generic_banned_market_class",
                    "source_metadata": {},
                },
            },
            {"market_id": "", "status": "REJECTED"},
        ]
    }


class DiscoveryPersistenceTests(unittest.TestCase):
    def test_migration_008_columns_and_metadata_coverage(self) -> None:
        conn = _database()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(revenue_poc_discovery_snapshots)")}
        self.assertTrue({"source_event_category", "source_tags", "source_series", "source_type", "reporting_class", "source_metadata"} <= columns)
        diagnostics = persist_discovery_snapshots(
            conn, discovery_result=_result(), run_id="run-1", timestamp_utc="2026-08-06T00:00:00Z", venue="polymarket"
        )
        self.assertEqual(diagnostics["discovered_rows"], 3)
        self.assertEqual(diagnostics["rows_prepared"], 2)
        self.assertEqual(diagnostics["missing_market_ids"], 1)
        self.assertEqual(diagnostics["rows_inserted"], 2)
        self.assertEqual(diagnostics["rows_ignored"], 0)
        self.assertEqual(diagnostics["source_metadata_rows"], 1)
        self.assertEqual(diagnostics["reporting_class_rows"], 2)
        row = conn.execute(
            "SELECT source_event_category,source_tags,source_series,source_type,reporting_class,source_metadata FROM revenue_poc_discovery_snapshots WHERE market_id='sports-1'"
        ).fetchone()
        self.assertEqual(row[0], "Sports")
        self.assertEqual(json.loads(row[1]), [{"label": "soccer"}])
        self.assertEqual(json.loads(row[2]), ["league-series", {"season": 2026, "stage": "final"}])
        self.assertEqual(row[3], "sports")
        self.assertEqual(row[4], "sports")
        self.assertEqual(json.loads(row[5])["event_category"], "Sports")
        self.assertFalse(conn.in_transaction)

    def test_scalar_series_remains_scalar(self) -> None:
        conn = _database()
        result = {"rows": [{"market_id": "scalar", "features": {"source_metadata": {"series": "league"}}}]}
        diagnostics = persist_discovery_snapshots(conn, discovery_result=result, run_id="run-scalar", timestamp_utc="t1", venue="polymarket")
        self.assertEqual(diagnostics["rows_inserted"], 1)
        self.assertEqual(conn.execute("SELECT source_series FROM revenue_poc_discovery_snapshots WHERE market_id='scalar'").fetchone()[0], "league")

    def test_canonical_json_is_stable_for_nested_values_and_sets(self) -> None:
        left = {"b": {3, 1}, "a": [{"z": 2, "y": 1}]}
        right = {"a": [{"y": 1, "z": 2}], "b": {1, 3}}
        self.assertEqual(canonical_json(left), canonical_json(right))
        self.assertEqual(canonical_json(("a", {"b": [2, 1]})), '["a",{"b":[2,1]}]')

    def test_null_metadata_is_stored_as_sql_null(self) -> None:
        conn = _database()
        result = {"rows": [{"market_id": "nulls", "features": {"source_metadata": {"tags": None, "series": None}}}]}
        diagnostics = persist_discovery_snapshots(conn, discovery_result=result, run_id="run-nulls", timestamp_utc="t1", venue="polymarket")
        self.assertEqual(diagnostics["rows_failed"], 0)
        row = conn.execute("SELECT source_tags,source_series FROM revenue_poc_discovery_snapshots WHERE market_id='nulls'").fetchone()
        self.assertIsNone(json.loads(row[0]))
        self.assertIsNone(row[1])

    def test_mixed_valid_and_unserializable_rows_are_isolated(self) -> None:
        conn = _database()

        class Unserializable:
            pass

        result = {"rows": [
            {"market_id": "good", "features": {"source_metadata": {"tags": ["a"]}}},
            {"market_id": "bad", "features": {"source_metadata": {"series": Unserializable()}}},
        ]}
        diagnostics = persist_discovery_snapshots(conn, discovery_result=result, run_id="run-mixed", timestamp_utc="t1", venue="polymarket")
        self.assertEqual(diagnostics["rows_prepared"], 2)
        self.assertEqual(diagnostics["rows_inserted"], 1)
        self.assertEqual(diagnostics["rows_ignored"], 0)
        self.assertEqual(diagnostics["rows_failed"], 1)
        self.assertIn("unsupported metadata type", diagnostics["persistence_error"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM revenue_poc_discovery_snapshots WHERE run_id='run-mixed'").fetchone()[0], 1)

    def test_partial_prior_insert_rerun_is_idempotent(self) -> None:
        conn = _database()
        first = persist_discovery_snapshots(conn, discovery_result=_result(), run_id="run-partial", timestamp_utc="t1", venue="polymarket")
        rerun_result = _result()
        rerun_result["rows"].append({"market_id": "new", "features": {"source_metadata": {"event_category": "Crypto"}, "reporting_class": "crypto"}})
        second = persist_discovery_snapshots(conn, discovery_result=rerun_result, run_id="run-partial", timestamp_utc="t2", venue="polymarket")
        self.assertEqual(first["rows_inserted"], 2)
        self.assertEqual(second["rows_inserted"], 1)
        self.assertEqual(second["rows_ignored"], 2)
        self.assertEqual(second["rows_failed"], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM revenue_poc_discovery_snapshots WHERE run_id='run-partial'").fetchone()[0], 3)
        self.assertEqual(conn.execute("SELECT timestamp_utc FROM revenue_poc_discovery_snapshots WHERE market_id='sports-1'").fetchone()[0], "t1")

    def test_unique_conflict_is_counted_and_historical_rows_remain(self) -> None:
        conn = _database()
        first = persist_discovery_snapshots(conn, discovery_result=_result(), run_id="run-1", timestamp_utc="t1", venue="polymarket")
        second = persist_discovery_snapshots(conn, discovery_result=_result(), run_id="run-1", timestamp_utc="t2", venue="polymarket")
        self.assertEqual(first["rows_inserted"], 2)
        self.assertEqual(second["rows_inserted"], 0)
        self.assertEqual(second["rows_ignored"], 2)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM revenue_poc_discovery_snapshots").fetchone()[0], 2)
        self.assertEqual(conn.execute("SELECT timestamp_utc FROM revenue_poc_discovery_snapshots WHERE market_id='sports-1'").fetchone()[0], "t1")

    def test_persistence_failure_is_reported(self) -> None:
        conn = _database()
        result = {"rows": [{"market_id": "bad", "status": None, "features": {}}]}
        diagnostics = persist_discovery_snapshots(conn, discovery_result=result, run_id="run-1", timestamp_utc="t1", venue="polymarket")
        self.assertEqual(diagnostics["rows_failed"], 1)
        self.assertIn("IntegrityError", diagnostics["persistence_error"])

    def test_reporting_mixes_historical_generic_and_new_source_rows(self) -> None:
        report = {
            "optimization": {"discovery": {"total_discovered": 2, "valid_contracts": 1, "shortlisted": 1}},
            "discovery_shadow": {"rows": [
                {"market_id": "old", "status": "REJECTED", "reason": "banned_market_class"},
                {"market_id": "new", "status": "REJECTED", "reason": "banned_market_class", "features": {"reporting_class": "sports", "classification_source": "source_metadata", "source_metadata": {"event_category": "Sports"}}},
            ]},
        }
        data = build_discovery_breakdown(report)
        self.assertEqual(data["banned_market_classes"]["sports"]["count"], 1)
        self.assertEqual(data["classification_coverage"]["scored_rows_with_source_classification"], 1)
        self.assertEqual(data["classification_coverage"]["historical_generic_rows"], 1)

    def test_reporting_decodes_serialized_metadata_fields(self) -> None:
        conn = _database()
        result = {"rows": [{"market_id": "decode", "features": {"reporting_class": "sports", "source_metadata": {"tags": [{"label": "soccer"}], "series": ["league"], "event_category": "Sports"}}}]}
        persist_discovery_snapshots(conn, discovery_result=result, run_id="run-decode", timestamp_utc="t1", venue="polymarket")
        row = conn.execute("SELECT source_tags,source_series,source_metadata FROM revenue_poc_discovery_snapshots WHERE market_id='decode'").fetchone()
        self.assertEqual(json.loads(row[0]), [{"label": "soccer"}])
        self.assertEqual(json.loads(row[1]), ["league"])
        self.assertEqual(json.loads(row[2])["event_category"], "Sports")


if __name__ == "__main__":
    unittest.main()
