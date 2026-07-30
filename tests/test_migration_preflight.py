from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.migration_preflight import inspect_db, provision, sqlite_backup


class MigrationPreflightTests(unittest.TestCase):
    def test_provision_uses_private_layout_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "Library" / "Application Support" / "SwarmEdge"
            paths = provision(target, "abc123")
            self.assertEqual(Path(paths["state"]).stat().st_mode & 0o777, 0o700)
            self.assertEqual((target / "releases" / "abc123").stat().st_mode & 0o777, 0o700)
            self.assertEqual((target.parent.parent / "Logs" / "SwarmEdge").stat().st_mode & 0o777, 0o700)

    def test_sqlite_backup_and_inspection_are_read_only_to_source(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = root / "source.sqlite"
            backup = root / "backup.sqlite"
            conn = sqlite3.connect(source)
            conn.executescript(
                "CREATE TABLE paper_trades(status TEXT);"
                "CREATE TABLE kv(key TEXT, value TEXT);"
                "INSERT INTO paper_trades VALUES ('OPEN'),('CLOSED'),('VOID');"
                "INSERT INTO kv VALUES ('infer_cursor','7');"
            )
            conn.commit()
            conn.close()
            before = source.read_bytes()
            sqlite_backup(source, backup)
            self.assertEqual(source.read_bytes(), before)
            report = inspect_db(backup)
            self.assertEqual(report["quick_check"], "ok")
            self.assertEqual(report["paper_counts"], {"CLOSED": 1, "OPEN": 1, "VOID": 1})
            self.assertEqual(report["infer_cursor"], "7")
            self.assertEqual(backup.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
