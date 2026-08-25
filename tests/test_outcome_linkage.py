from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from loop_engine.shadow import insert_shadow_forecast, resolve_shadow_forecast
from outcome_linkage import (
    ResolutionConflictError,
    ensure_resolution_schema,
    linked_forecasts,
    record_market_resolution,
)


SCHEMA = Path(__file__).resolve().parent.parent / "migrations" / "005_phase3_3_shadow_forecasts.sql"


def make_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def add_forecast(conn: sqlite3.Connection, run_id: str, market_id: str) -> int:
    return insert_shadow_forecast(
        conn,
        {
            "run_id": run_id,
            "market_id": market_id,
            "slug": market_id,
            "timestamp_utc": "2026-08-20T00:00:00+00:00",
            "venue": "polymarket",
            "market_probability": 0.4,
            "model_probability": 0.6,
            "side": "YES",
            "metadata": {"fixture": True},
        },
        production_threshold=0.02,
    ).forecast_id


class OutcomeLinkageTests(unittest.TestCase):
    def test_commit_false_defers_canonical_resolution_until_caller_commit(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".sqlite") as database:
            conn = sqlite3.connect(database.name)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA.read_text(encoding="utf-8"))
            ensure_resolution_schema(conn)
            forecast_id = add_forecast(conn, "run-1", "m-1")
            conn.commit()
            observer = sqlite3.connect(database.name)

            self.assertTrue(resolve_shadow_forecast(
                conn, forecast_id, "YES",
                resolved_at_utc="2026-08-25T00:00:00Z", commit=False,
            ))
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0], 0)
            self.assertEqual(observer.execute("SELECT status FROM shadow_forecasts").fetchone()[0], "OPEN")

            conn.commit()
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0], 1)
            self.assertEqual(observer.execute("SELECT status FROM shadow_forecasts").fetchone()[0], "RESOLVED")
            observer.close()

    def test_caller_rollback_removes_canonical_and_forecast_resolution(self) -> None:
        conn = make_db()
        ensure_resolution_schema(conn)
        forecast_id = add_forecast(conn, "run-1", "m-1")
        conn.commit()

        self.assertTrue(resolve_shadow_forecast(
            conn, forecast_id, "NO",
            resolved_at_utc="2026-08-25T00:00:00Z", commit=False,
        ))
        conn.rollback()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT status FROM shadow_forecasts").fetchone()[0], "OPEN")

    def test_first_resolution_persists_and_identical_replay_is_idempotent(self) -> None:
        conn = make_db()
        first = record_market_resolution(
            conn, venue="polymarket", market_id="m-1", outcome="YES",
            resolved_at_utc="2026-08-25T00:00:00Z", resolution_source="fixture",
            source_reference="ref-1", recorded_at_utc="2026-08-25T00:01:00Z",
            provenance_metadata={"closed": True},
        )
        second = record_market_resolution(
            conn, venue="polymarket", market_id="m-1", outcome="YES",
            resolved_at_utc="2026-08-25T00:00:00Z", resolution_source="fixture",
            source_reference="ref-1", recorded_at_utc="2026-08-25T00:01:00Z",
            provenance_metadata={"closed": True},
        )
        self.assertTrue(first.inserted)
        self.assertFalse(second.inserted)
        self.assertEqual(first.id, second.id)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_resolutions").fetchone()[0], 1)

    def test_conflict_fails_closed_and_original_is_preserved(self) -> None:
        conn = make_db()
        record_market_resolution(
            conn, venue="polymarket", market_id="m-1", outcome="YES",
            resolved_at_utc="2026-08-25T00:00:00Z", resolution_source="fixture",
        )
        with self.assertRaises(ResolutionConflictError):
            record_market_resolution(
                conn, venue="polymarket", market_id="m-1", outcome="NO",
                resolved_at_utc="2026-08-25T00:02:00Z", resolution_source="other",
            )
        self.assertEqual(conn.execute("SELECT resolved_outcome FROM market_resolutions").fetchone()[0], "YES")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE market_resolutions SET resolved_outcome='NO'")
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM market_resolutions")

    def test_all_historical_forecasts_link_to_one_canonical_outcome(self) -> None:
        conn = make_db()
        add_forecast(conn, "run-1", "m-1")
        add_forecast(conn, "run-2", "m-1")
        before = [tuple(row) for row in conn.execute("SELECT * FROM shadow_forecasts ORDER BY id")]
        self.assertTrue(all(row["canonical_resolved_outcome"] is None for row in linked_forecasts(conn, venue="polymarket", market_id="m-1")))
        record_market_resolution(
            conn, venue="polymarket", market_id="m-1", outcome="NO",
            resolved_at_utc="2026-08-26T00:00:00Z", resolution_source="fixture",
        )
        linked = linked_forecasts(conn, venue="polymarket", market_id="m-1")
        self.assertEqual(len(linked), 2)
        self.assertEqual({row["canonical_resolved_outcome"] for row in linked}, {"NO"})
        self.assertEqual(before, [tuple(row) for row in conn.execute("SELECT * FROM shadow_forecasts ORDER BY id")])
        forecast_ts = conn.execute("SELECT MIN(timestamp_utc) FROM shadow_forecasts").fetchone()[0]
        resolution_ts = conn.execute("SELECT resolved_at_utc FROM market_resolutions").fetchone()[0]
        self.assertLess(forecast_ts, resolution_ts)


if __name__ == "__main__":
    unittest.main()
