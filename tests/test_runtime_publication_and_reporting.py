from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import shutil
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from reporting import paper_dashboard
from scripts import export_data, integrity_check, morning_status
from swarm_edge_io import load_paper_trades_export, merge_notes_blob


def _create_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE paper_trades (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id TEXT NOT NULL,
          ts_utc TEXT NOT NULL,
          market_id TEXT NOT NULL,
          question TEXT NOT NULL,
          venue TEXT NOT NULL,
          side TEXT NOT NULL,
          consensus_p_yes REAL NOT NULL,
          disagreement REAL NOT NULL,
          size_usd REAL NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL,
          resolved_outcome TEXT,
          p_yes REAL NOT NULL,
          edge REAL NOT NULL,
          brier REAL,
          notes TEXT NOT NULL
        )
        """
    )
    conn.commit()
    conn.close()


def _insert_trade(
    conn: sqlite3.Connection,
    *,
    status: str,
    side: str,
    resolved_outcome: str | None,
    notes: str,
    market_id: str,
    trade_id: int,
) -> None:
    conn.execute(
        """
        INSERT INTO paper_trades
        (id,run_id,ts_utc,market_id,question,venue,side,consensus_p_yes,disagreement,
         size_usd,reason,status,resolved_outcome,p_yes,edge,brier,notes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            trade_id,
            f"run-{trade_id}",
            "2026-01-01T00:00:00+00:00",
            market_id,
            f"Question {trade_id}?",
            "polymarket",
            side,
            0.6,
            0.1,
            100.0,
            "infer",
            status,
            resolved_outcome,
            0.6,
            0.1,
            0.04 if status == "CLOSED" else None,
            notes,
        ),
    )


class RuntimePublicationAndReportingTests(unittest.TestCase):
    def test_merge_notes_blob_handles_appended_resolution(self) -> None:
        merged = merge_notes_blob(
            json.dumps({"p_yes_market": 0.4, "llm": {"confidence": 0.8}})
            + "\n"
            + json.dumps({"resolution": {"profit_usd": 12.5, "lookup_source": "snapshot_id"}})
        )
        self.assertAlmostEqual(merged["p_yes_market"], 0.4)
        self.assertAlmostEqual(merged["profit_usd"], 12.5)
        self.assertEqual(merged["lookup_source"], "snapshot_id")
        self.assertIn("resolution", merged)

    def test_export_is_local_only_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            signals_dir = root / "signals"
            db_path = root / "runs.sqlite"
            data_dir.mkdir()
            signals_dir.mkdir()
            _create_db(db_path)
            conn = sqlite3.connect(db_path)
            _insert_trade(
                conn,
                status="OPEN",
                side="YES",
                resolved_outcome=None,
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="market-a",
                trade_id=1,
            )
            conn.commit()
            conn.close()
            (signals_dir / "infer_diagnostics.json").write_text(
                json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "rows": []})
            )

            with mock.patch.object(export_data, "DB_PATH", db_path), mock.patch.object(
                export_data, "DATA_DIR", data_dir
            ), mock.patch.object(
                export_data, "TRADES_OUT", data_dir / "paper_trades.json"
            ), mock.patch.object(
                export_data, "DIAG_SRC", signals_dir / "infer_diagnostics.json"
            ), mock.patch.object(
                export_data, "DIAG_OUT", data_dir / "infer_diagnostics.json"
            ), mock.patch.object(export_data, "CUTOFF", "2025-01-01T00:00"), mock.patch.object(
                export_data, "PUBLISH_ENABLED", False
            ), mock.patch(
                "scripts.export_data.subprocess.run"
            ) as run_mock:
                export_data.main()

            self.assertFalse(run_mock.called)
            payload = json.loads((data_dir / "paper_trades.json").read_text())
            self.assertIn("generated_at_utc", payload)
            self.assertEqual(payload["source_db_path"], str(db_path))
            self.assertEqual(len(payload["records"]), 1)

    def test_publication_stages_only_data_files_when_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            data_dir = root / "data"
            signals_dir = root / "signals"
            db_path = root / "runs.sqlite"
            data_dir.mkdir()
            signals_dir.mkdir()
            _create_db(db_path)
            conn = sqlite3.connect(db_path)
            _insert_trade(
                conn,
                status="OPEN",
                side="YES",
                resolved_outcome=None,
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="market-a",
                trade_id=1,
            )
            conn.commit()
            conn.close()
            (signals_dir / "infer_diagnostics.json").write_text(
                json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "rows": []})
            )

            fake_run = mock.Mock(return_value=mock.Mock(returncode=0, stderr="", stdout=""))
            with mock.patch.object(export_data, "DB_PATH", db_path), mock.patch.object(
                export_data, "DATA_DIR", data_dir
            ), mock.patch.object(
                export_data, "TRADES_OUT", data_dir / "paper_trades.json"
            ), mock.patch.object(
                export_data, "DIAG_SRC", signals_dir / "infer_diagnostics.json"
            ), mock.patch.object(
                export_data, "DIAG_OUT", data_dir / "infer_diagnostics.json"
            ), mock.patch.object(export_data, "CUTOFF", "2025-01-01T00:00"), mock.patch.object(
                export_data, "PUBLISH_ENABLED", True
            ), mock.patch(
                "scripts.export_data.subprocess.run", side_effect=fake_run
            ):
                export_data.export_trades()
                export_data.export_diagnostics()
                export_data.git_push()

            staged = [
                call.args[0]
                for call in fake_run.call_args_list
                if call.args and call.args[0][:2] == ["git", "add"]
            ]
            self.assertTrue(staged)
            self.assertTrue(all(".env" not in " ".join(args) for args in staged))

    def test_morning_status_reports_sqlite_states(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "runs.sqlite"
            _create_db(db_path)
            conn = sqlite3.connect(db_path)
            _insert_trade(
                conn,
                status="OPEN",
                side="YES",
                resolved_outcome=None,
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="open-market",
                trade_id=1,
            )
            _insert_trade(
                conn,
                status="PENDING",
                side="YES",
                resolved_outcome=None,
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="pending-market",
                trade_id=2,
            )
            _insert_trade(
                conn,
                status="CLOSED",
                side="YES",
                resolved_outcome="YES",
                notes=(
                    json.dumps({"p_yes_market": 0.4})
                    + "\n"
                    + json.dumps({"resolution": {"profit_usd": 12.5}})
                ),
                market_id="closed-market",
                trade_id=3,
            )
            _insert_trade(
                conn,
                status="VOID",
                side="NO",
                resolved_outcome="VOID",
                notes=json.dumps({}),
                market_id="void-market",
                trade_id=4,
            )
            conn.commit()
            conn.close()

            with mock.patch.object(morning_status, "DB_PATH", db_path):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    morning_status.check_positions()
            out = buf.getvalue()
            self.assertIn("POSITIONS  [1 open  1 pending  1 closed  1 void]", out)
            self.assertIn("closed-market", out)
            self.assertIn("profit=$+12.50", out)

    def test_paper_dashboard_counts_closed_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "runs.sqlite"
            _create_db(db_path)
            conn = sqlite3.connect(db_path)
            _insert_trade(
                conn,
                status="OPEN",
                side="YES",
                resolved_outcome=None,
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="open-market",
                trade_id=1,
            )
            _insert_trade(
                conn,
                status="CLOSED",
                side="YES",
                resolved_outcome="YES",
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="closed-market",
                trade_id=2,
            )
            _insert_trade(
                conn,
                status="VOID",
                side="NO",
                resolved_outcome="VOID",
                notes=json.dumps({}),
                market_id="void-market",
                trade_id=3,
            )
            conn.commit()
            conn.close()

            argv = ["paper_dashboard.py", "--db", str(db_path), "--limit", "3"]
            with mock.patch.object(sys, "argv", argv):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    paper_dashboard.main()
            out = buf.getvalue()
            self.assertIn("- closed_trades:     1", out)
            self.assertIn("- resolved_trades:   1", out)

    def test_run_live_wrapper_is_local_only_with_publish_disabled(self) -> None:
        env = os.environ.copy()
        env.update(
            {
                "PYTHON_BIN": "/usr/bin/true",
                "LOOPS": "1",
                "SLEEP_SECS": "0",
                "RESOLVE_EVERY": "1",
                "EXPORT_EVERY": "1",
                "DISCOVER_EVERY": "1",
                "SWARM_EDGE_PUBLISH_ENABLED": "0",
                "SWARM_EDGE_WATCHLIST_APPLY": "0",
            }
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            shutil.copy2(
                Path(__file__).resolve().parent.parent / "scripts" / "run_live.sh",
                root / "scripts" / "run_live.sh",
            )
            result = subprocess.run(
                ["bash", "scripts/run_live.sh"],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("auto-export data", result.stdout)
        self.assertIn("watchlist apply disabled", result.stdout)
        self.assertNotIn("PIPELINE", result.stdout)

    def test_export_entrypoint_works_outside_checkout_with_absolute_runtime_paths(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        for cwd in (repo_root, Path("/tmp")):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                (root / "scripts").mkdir()
                shutil.copy2(repo_root / "scripts" / "export_data.py", root / "scripts" / "export_data.py")
                shutil.copy2(repo_root / "swarm_edge_runtime.py", root / "swarm_edge_runtime.py")
                db_path = root / "runs.sqlite"
                data_dir = root / "data"
                signals_dir = root / "signals"
                data_dir.mkdir()
                signals_dir.mkdir()
                _create_db(db_path)
                conn = sqlite3.connect(db_path)
                _insert_trade(
                    conn,
                    status="OPEN",
                    side="YES",
                    resolved_outcome=None,
                    notes=json.dumps({"p_yes_market": 0.4}),
                    market_id="fixture-market",
                    trade_id=1,
                )
                conn.commit()
                conn.close()
                (signals_dir / "infer_diagnostics.json").write_text(
                    json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "rows": []})
                )
                env = os.environ.copy()
                env.update(
                    {
                        "BGL_DB_PATH": str(db_path),
                        "BGL_LOG_DIR": str(root / "logs"),
                        "BGL_SIGNALS_DIR": str(signals_dir),
                        "BGL_REPORT_DIR": str(root / "reports"),
                        "BGL_BACKUP_DIR": str(root / "backups"),
                        "BGL_RUNTIME_DIR": str(root / "runtime"),
                        "BGL_WATCHLIST_PATH": str(root / "watchlist.json"),
                        "BGL_RUNTIME_ENV_FILE": str(root / "runtime.env"),
                        "SWARM_EDGE_PUBLISH_ENABLED": "0",
                        "PYTHONDONTWRITEBYTECODE": "1",
                    }
                )
                env.pop("PYTHONPATH", None)
                result = subprocess.run(
                    [sys.executable, str(root / "scripts" / "export_data.py")],
                    cwd=cwd,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("publication disabled", result.stdout)
                self.assertNotIn("ModuleNotFoundError", result.stdout + result.stderr)
                self.assertTrue((data_dir / "paper_trades.json").exists())
                self.assertTrue((data_dir / "infer_diagnostics.json").exists())

    def test_failed_export_commit_unstages_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "data").mkdir()
            trades_out = root / "data" / "paper_trades.json"
            diag_out = root / "data" / "infer_diagnostics.json"
            trades_out.write_text("{}")
            diag_out.write_text("{}")
            responses = iter([0, 1, 1, 0])

            def fake_run(cmd, **kwargs):
                return mock.Mock(returncode=next(responses), stderr="commit failed", stdout="")

            run_mock = mock.Mock(side_effect=fake_run)
            with mock.patch.object(export_data, "ROOT", root), mock.patch.object(
                export_data, "TRADES_OUT", trades_out
            ), mock.patch.object(export_data, "DIAG_OUT", diag_out), mock.patch.object(
                export_data, "PUBLISH_ENABLED", True
            ), mock.patch("scripts.export_data.subprocess.run", run_mock):
                export_data.git_push()

            # The reset command is the final subprocess call after commit failure.
            self.assertEqual(
                ["git", "reset", "--quiet", "--", "data/paper_trades.json", "data/infer_diagnostics.json"],
                run_mock.call_args_list[-1].args[0],
            )

    def test_publication_refused_in_production_worktree(self) -> None:
        with mock.patch.object(export_data, "PUBLISH_ENABLED", True), mock.patch.dict(
            os.environ, {"BGL_PRODUCTION_MODE": "1"}, clear=False
        ), mock.patch("scripts.export_data.subprocess.run") as run_mock:
            export_data.git_push()
        run_mock.assert_not_called()

    def test_export_entrypoint_works_with_compatibility_defaults(self) -> None:
        repo_root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "scripts").mkdir()
            shutil.copy2(repo_root / "scripts" / "export_data.py", root / "scripts" / "export_data.py")
            shutil.copy2(repo_root / "swarm_edge_runtime.py", root / "swarm_edge_runtime.py")
            (root / "memory").mkdir()
            (root / "signals").mkdir()
            _create_db(root / "memory" / "runs.sqlite")
            (root / "signals" / "infer_diagnostics.json").write_text(
                json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "rows": []})
            )
            env = os.environ.copy()
            for key in (
                "BGL_DB_PATH", "BGL_LOG_DIR", "BGL_SIGNALS_DIR", "BGL_REPORT_DIR",
                "BGL_BACKUP_DIR", "BGL_RUNTIME_DIR", "BGL_WATCHLIST_PATH",
                "BGL_RUNTIME_ENV_FILE", "BGL_RUNTIME_MODE", "BGL_PRODUCTION_MODE",
            ):
                env.pop(key, None)
            env["SWARM_EDGE_PUBLISH_ENABLED"] = "0"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            result = subprocess.run(
                [sys.executable, str(root / "scripts" / "export_data.py")],
                cwd=Path("/tmp"),
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("publication disabled", result.stdout)
            self.assertTrue((root / "data" / "paper_trades.json").exists())
            self.assertTrue((root / "data" / "infer_diagnostics.json").exists())

    def test_export_and_database_fingerprint_remain_stable(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "runs.sqlite"
            data_dir = root / "data"
            signals_dir = root / "signals"
            data_dir.mkdir()
            signals_dir.mkdir()
            _create_db(db_path)
            conn = sqlite3.connect(db_path)
            _insert_trade(
                conn,
                status="OPEN",
                side="YES",
                resolved_outcome=None,
                notes=json.dumps({"p_yes_market": 0.4}),
                market_id="market-a",
                trade_id=1,
            )
            conn.commit()
            conn.close()
            (signals_dir / "infer_diagnostics.json").write_text(
                json.dumps({"ts_utc": "2026-01-01T00:00:00+00:00", "rows": []})
            )
            before = hashlib.sha256(db_path.read_bytes()).hexdigest()

            with mock.patch.object(export_data, "DB_PATH", db_path), mock.patch.object(
                export_data, "DATA_DIR", data_dir
            ), mock.patch.object(
                export_data, "TRADES_OUT", data_dir / "paper_trades.json"
            ), mock.patch.object(
                export_data, "DIAG_SRC", signals_dir / "infer_diagnostics.json"
            ), mock.patch.object(
                export_data, "DIAG_OUT", data_dir / "infer_diagnostics.json"
            ), mock.patch.object(export_data, "CUTOFF", "2025-01-01T00:00"), mock.patch.object(
                export_data, "PUBLISH_ENABLED", False
            ):
                export_data.main()

            after = hashlib.sha256(db_path.read_bytes()).hexdigest()
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
