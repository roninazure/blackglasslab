from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from swarm_edge_runtime import PROJECT_ROOT, get_runtime_paths


class RuntimePathTests(unittest.TestCase):
    def test_compatibility_defaults_match_checkout_layout(self) -> None:
        paths = get_runtime_paths({}, load_env=False)
        self.assertEqual(paths.root, PROJECT_ROOT)
        self.assertEqual(paths.db_path, PROJECT_ROOT / "memory" / "runs.sqlite")
        self.assertEqual(paths.log_dir, PROJECT_ROOT / "logs")
        self.assertEqual(paths.signals_dir, PROJECT_ROOT / "signals")
        self.assertEqual(paths.report_dir, PROJECT_ROOT / "reports")
        self.assertEqual(paths.backup_dir, PROJECT_ROOT / "backups")
        self.assertEqual(paths.runtime_dir, PROJECT_ROOT / "runtime")
        self.assertEqual(
            paths.watchlist_path,
            PROJECT_ROOT / "markets" / "polymarket_watchlist.json",
        )

    def test_relative_compatibility_overrides_resolve_from_project_root(self) -> None:
        paths = get_runtime_paths(
            {
                "BGL_DB_PATH": "memory/runs.sqlite",
                "BGL_LOG_DIR": "logs",
                "BGL_SIGNALS_DIR": "signals",
                "BGL_REPORT_DIR": "reports",
                "BGL_BACKUP_DIR": "backups",
                "BGL_RUNTIME_DIR": "runtime",
                "BGL_WATCHLIST_PATH": "markets/polymarket_watchlist.json",
            },
            load_env=False,
        )
        self.assertEqual(paths.db_path, PROJECT_ROOT / "memory" / "runs.sqlite")
        self.assertEqual(paths.report_dir, PROJECT_ROOT / "reports")

    def test_production_rejects_relative_path_overrides(self) -> None:
        for variable in (
            "BGL_DB_PATH",
            "BGL_LOG_DIR",
            "BGL_SIGNALS_DIR",
            "BGL_REPORT_DIR",
            "BGL_BACKUP_DIR",
            "BGL_RUNTIME_DIR",
            "BGL_WATCHLIST_PATH",
            "BGL_RUNTIME_ENV_FILE",
        ):
            with self.subTest(variable=variable):
                with self.assertRaisesRegex(ValueError, variable):
                    get_runtime_paths(
                        {
                            "BGL_RUNTIME_MODE": "production",
                            variable: "relative/path",
                        },
                        load_env=False,
                    )

    def test_runtime_env_file_precedes_defaults_but_not_explicit_environment(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            env_file = root / "runtime.env"
            env_file.write_text(
                "BGL_RUNTIME_MODE=production\n"
                f"BGL_DB_PATH={root / 'db' / 'runs.sqlite'}\n"
                f"BGL_LOG_DIR={root / 'logs'}\n"
                f"BGL_SIGNALS_DIR={root / 'signals'}\n"
                f"BGL_REPORT_DIR={root / 'reports'}\n"
                f"BGL_BACKUP_DIR={root / 'backups'}\n"
                f"BGL_RUNTIME_DIR={root / 'runtime'}\n"
                f"BGL_WATCHLIST_PATH={root / 'watchlist.json'}\n",
                encoding="utf-8",
            )
            environ = {
                "BGL_RUNTIME_ENV_FILE": str(env_file),
                "BGL_DB_PATH": str(root / "explicit.sqlite"),
            }
            paths = get_runtime_paths(environ)
            self.assertEqual(paths.db_path, (root / "explicit.sqlite").resolve())
            self.assertEqual(paths.log_dir, (root / "logs").resolve())
            self.assertEqual(paths.runtime_env_file, env_file.resolve())

    def test_components_share_resolved_database_and_directories(self) -> None:
        from live_runner import DB_PATH as live_db, SIGNALS_DIR as live_signals
        from reporting import leaderboard, paper_dashboard
        from scripts import (
            approve_trades,
            calibration_baseline,
            export_data,
            integrity_check,
            manage_watchlist,
            morning_status,
            resolve_paper_trades,
            void_trades,
            watch_resolutions,
        )

        expected = get_runtime_paths()
        self.assertEqual(Path(live_db), expected.db_path)
        self.assertEqual(live_signals, expected.signals_dir)
        for value in (
            leaderboard.DB_PATH,
            paper_dashboard.DB_PATH,
            resolve_paper_trades.DB_PATH,
            watch_resolutions.DB_PATH,
        ):
            self.assertEqual(Path(value), expected.db_path)
        for value in (
            approve_trades.DB_PATH,
            calibration_baseline.DEFAULT_DB,
            export_data.DB_PATH,
            integrity_check.DB_PATH,
            manage_watchlist.DB_PATH,
            morning_status.DB_PATH,
            void_trades.DB_PATH,
        ):
            self.assertEqual(Path(value), expected.db_path)
        self.assertEqual(export_data.DIAG_SRC.parent, expected.signals_dir)
        self.assertEqual(manage_watchlist.REPORTS_DIR, expected.report_dir)
        self.assertEqual(morning_status.LOG_PATH.parent, expected.log_dir)

    def test_entrypoint_runs_from_unrelated_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            env = os.environ.copy()
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, str(PROJECT_ROOT / "scripts" / "resolve_paper_trades.py"), "--help"],
                cwd=td,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--db", result.stdout)
            self.assertFalse((Path(td) / "memory").exists())


if __name__ == "__main__":
    unittest.main()
