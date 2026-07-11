from __future__ import annotations

import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from scripts import resolve_paper_trades as resolver


class ResolverTests(unittest.TestCase):
    def test_snapshot_id_fallback_and_dry_run_do_not_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "runs.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE paper_trades (
                  id INTEGER PRIMARY KEY, market_id TEXT, consensus_p_yes REAL,
                  p_yes REAL, side TEXT, size_usd REAL, notes TEXT,
                  status TEXT, venue TEXT, resolved_outcome TEXT, brier REAL
                )
                """
            )
            original_notes = json.dumps(
                {"snapshot": {"id": 1126854}, "p_yes_market": 0.4}
            )
            conn.execute(
                """
                INSERT INTO paper_trades
                (id,market_id,consensus_p_yes,p_yes,side,size_usd,notes,status,venue)
                VALUES (6,'stale-slug',0.7,0.7,'YES',100,?,'OPEN','polymarket')
                """,
                (original_notes,),
            )
            conn.commit()
            before = conn.execute("SELECT * FROM paper_trades").fetchall()
            conn.close()

            resolved = {
                "id": "1126854",
                "active": False,
                "closed": True,
                "outcomes": ["Yes", "No"],
                "outcomePrices": ["1", "0"],
            }
            output = io.StringIO()
            argv = [
                "resolve_paper_trades.py",
                "--db",
                str(db_path),
                "--dry-run",
                "--sleep",
                "0",
            ]
            with (
                mock.patch.object(sys, "argv", argv),
                mock.patch.object(
                    resolver, "fetch_market_by_slug", side_effect=ValueError("not found")
                ),
                mock.patch.object(
                    resolver, "fetch_market_by_id", return_value=resolved
                ) as fetch_by_id,
                redirect_stdout(output),
            ):
                self.assertEqual(resolver.main(), 0)

            fetch_by_id.assert_called_once_with(1126854, timeout_s=20)
            self.assertIn("lookup_source=slug lookup_failed=not found", output.getvalue())
            self.assertIn("lookup_source=snapshot_id CLOSED", output.getvalue())

            conn = sqlite3.connect(db_path)
            after = conn.execute("SELECT * FROM paper_trades").fetchall()
            conn.close()
            self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main()
