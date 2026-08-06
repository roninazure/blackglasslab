from __future__ import annotations

import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.migration_preflight import (
    create_release,
    harden_release_permissions,
    inspect_db,
    provision,
    sqlite_backup,
)


class MigrationPreflightTests(unittest.TestCase):
    def test_release_hardening_repairs_shebang_entrypoints_and_removes_owner_write(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "release"
            (root / "bin").mkdir(parents=True)
            (root / "scripts").mkdir()
            (root / "package").mkdir()
            (root / "bin" / "swarm-edge").write_text("#!/bin/sh\n", encoding="utf-8")
            (root / "scripts" / "run_live.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (root / "scripts" / "module.py").write_text("print('ok')\n", encoding="utf-8")
            (root / "package" / "data.txt").write_text("data\n", encoding="utf-8")
            for path in root.rglob("*"):
                if path.is_file():
                    path.chmod(0o666)

            harden_release_permissions(root)

            self.assertTrue((root / "bin" / "swarm-edge").stat().st_mode & 0o111)
            self.assertTrue((root / "scripts" / "run_live.sh").stat().st_mode & 0o111)
            self.assertFalse((root / "scripts" / "module.py").stat().st_mode & 0o111)
            self.assertEqual((root / "package" / "data.txt").stat().st_mode & 0o777, 0o444)
            self.assertEqual((root / "bin").stat().st_mode & 0o777, 0o555)
            self.assertEqual((root / "scripts").stat().st_mode & 0o777, 0o555)

    def test_create_release_preserves_entrypoints_from_git_archive(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source"
            source.mkdir()
            subprocess.run(["git", "init", "-q", str(source)], check=True)
            (source / "scripts").mkdir()
            (source / "scripts" / "run_live.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            (source / "scripts" / "run_live.sh").chmod(0o644)
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
            subprocess.run(
                ["git", "-C", str(source), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "fixture"],
                check=True,
            )
            sha = subprocess.check_output(["git", "-C", str(source), "rev-parse", "HEAD"], text=True).strip()
            target = Path(td) / "release"
            create_release(source, target, sha)
            self.assertTrue((target / "scripts" / "run_live.sh").stat().st_mode & 0o111)
            self.assertEqual((target / "scripts").stat().st_mode & 0o777, 0o555)

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
