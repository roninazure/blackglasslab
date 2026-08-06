from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from operator_console.app import OperatorConsoleApp, PositionDetailScreen
from operator_console.cli import COMMANDS, main, parser
from operator_console.data import OperatorDataSource
from operator_console.models import ConsoleSnapshot
from revenue_poc.config import RevenueConfig
from revenue_poc.repository import apply_schema
from revenue_poc.service import RevenuePOCService
from swarm_edge_runtime import PATH_ENV_VARS, RuntimePaths, get_runtime_paths

ROOT = Path(__file__).resolve().parent.parent


def _paths(root: Path) -> RuntimePaths:
    state = root / "state"
    logs = root / "logs"
    signals = state / "signals"
    for path in (
        state,
        logs,
        signals,
        state / "reports",
        state / "exports",
        state / "backups",
        state / "run",
    ):
        path.mkdir(parents=True, exist_ok=True)
    (logs / "infer_loop.log").write_text(
        "== 2026-08-04T11:00:00Z : infer loop == (cycle 1)\n"
        "LIVE_RUNNER OK candidates=0\n"
        "RESOLVER SUMMARY: checked=4 closed=0\n"
        "  [export] publication disabled (SWARM_EDGE_PUBLISH_ENABLED=0)\n",
        encoding="utf-8",
    )
    (logs / "infer_loop.err.log").write_text("", encoding="utf-8")
    (signals / "infer_pipeline_report.json").write_text(
        json.dumps(
            {
                "ts_utc": "2026-08-04T11:00:00+00:00",
                "summary": {
                    "watchlist_total": 22,
                    "finalized_markets": 22,
                    "fetch_attempted": 20,
                    "fetch_failed": 1,
                    "opportunity_scored": 12,
                    "diagnostics_written": 10,
                    "llm_attempted": 2,
                    "skeptic_attempted": 1,
                    "candidates_generated": 0,
                    "budget_skipped": 8,
                },
                "markets": [
                    {
                        "market_id": "a",
                        "decision": "REJECT",
                        "reason": "min_edge_abs",
                        "final_stage": "edge_filter",
                    },
                    {
                        "market_id": "b",
                        "decision": "SKIP",
                        "reason": "budget_skipped",
                        "final_stage": "budget",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return RuntimePaths(
        root=root,
        db_path=state / "runs.sqlite",
        log_dir=logs,
        signals_dir=signals,
        report_dir=state / "reports",
        export_dir=state / "exports",
        backup_dir=state / "backups",
        runtime_dir=state / "run",
        watchlist_path=state / "watchlist.json",
        runtime_env_file=root / "runtime.env",
    )


def _create_database(
    path: Path, *, positions: bool = True, resolve_one: bool = False
) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(
        """
        CREATE TABLE paper_trades (id INTEGER PRIMARY KEY, market_id TEXT, status TEXT);
        CREATE TABLE shadow_forecasts (
          id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, timestamp_utc TEXT NOT NULL,
          venue TEXT NOT NULL, market_id TEXT NOT NULL, question TEXT NOT NULL,
          category TEXT NOT NULL, market_probability REAL NOT NULL,
          model_probability REAL NOT NULL, absolute_edge REAL NOT NULL,
          production_decision TEXT NOT NULL, rejection_reason TEXT,
          time_to_resolution_days REAL, market_end_date TEXT,
          llm_used INTEGER NOT NULL, metadata TEXT NOT NULL
        );
        """
    )
    apply_schema(conn)
    service = RevenuePOCService(conn, RevenueConfig())
    service.initialize()
    if positions:
        for index in range(1, 4):
            conn.execute(
                "INSERT INTO shadow_forecasts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    index,
                    f"run-{index}",
                    f"2026-08-0{index}T10:00:00+00:00",
                    "polymarket",
                    f"market-{index}",
                    f"Will market {index} resolve YES?",
                    "politics" if index < 3 else "crypto",
                    0.50,
                    0.65,
                    0.15,
                    "rejected",
                    "min_edge_abs",
                    30.0,
                    "2026-09-04T00:00:00+00:00",
                    1,
                    json.dumps(
                        {
                            "spread": 0.02,
                            "market_snapshot": {
                                "best_bid": 0.49,
                                "best_ask": 0.51,
                                "liquidity": 10_000,
                            },
                        }
                    ),
                ),
            )
        conn.commit()
        service.ingest_shadow_forecasts()
        service.mark_position(
            1,
            {
                "best_bid": 0.70,
                "best_ask": 0.71,
                "bid_depth_usd": 100,
                "ask_depth_usd": 110,
                "depth_source": "fixture_top_level",
                "fee_rate": 0.0,
                "fee_source": "fixture",
                "quote_timestamp_utc": "2026-08-04T11:55:00+00:00",
                "quote_source": "fixture",
            },
        )
        service.mark_position(
            2,
            {
                "best_bid": 0.30,
                "best_ask": 0.31,
                "bid_depth_usd": 100,
                "ask_depth_usd": 110,
                "depth_source": "fixture_top_level",
                "fee_rate": 0.0,
                "fee_source": "fixture",
                "quote_timestamp_utc": "2026-08-04T09:00:00+00:00",
                "quote_source": "fixture",
            },
        )
        if resolve_one:
            service.resolve_position(3, "YES", "2026-08-04T11:58:00+00:00")
    conn.close()


def _launchctl() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        ["launchctl", "print"],
        0,
        "state = running\npid = 4242\n",
        "",
    )


def _source(paths: RuntimePaths, *, now: datetime | None = None) -> OperatorDataSource:
    return OperatorDataSource(
        paths,
        environ={"SLEEP_SECS": "3600", "SWARM_EDGE_ROOT": str(paths.root)},
        now=lambda: now or datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        launchctl=_launchctl,
    )


class OperatorDataTests(unittest.TestCase):
    def test_read_only_snapshot_preserves_database_bytes_and_rejects_writes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            _create_database(paths.db_path)
            before = hashlib.sha256(paths.db_path.read_bytes()).hexdigest()
            source = _source(paths)
            snapshot = source.read()
            after = hashlib.sha256(paths.db_path.read_bytes()).hexdigest()
            self.assertEqual(before, after)
            self.assertEqual(snapshot.system.database_status, "OK / READ-ONLY")
            with source._connect() as conn, self.assertRaises(sqlite3.OperationalError):
                conn.execute(
                    "INSERT INTO revenue_poc_decisions(timestamp_utc,decision,reason,expected_lost_pnl_usd,details) VALUES ('x','x','x',0,'{}')"
                )

    def test_database_unavailable_fails_safe(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            snapshot = _source(paths).read()
            self.assertEqual(snapshot.system.database_status, "UNAVAILABLE")
            self.assertIn("unable to open database", snapshot.system.message or "")
            self.assertEqual(snapshot.portfolio.equity, 0)

    def test_stale_data_retains_values_and_marks_quote_stale(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            _create_database(paths.db_path)
            source = _source(paths, now=datetime(2026, 8, 5, 15, 0, tzinfo=UTC))
            first = source.read()
            self.assertEqual(first.system.cycle_state, "STALE")
            self.assertTrue(
                any(position.quote_state == "STALE" for position in first.positions)
            )
            paths.db_path.rename(paths.db_path.with_suffix(".offline"))
            retained = source.read()
            self.assertIn("LAST VALUE RETAINED", retained.system.database_status)
            self.assertEqual(retained.portfolio, first.portfolio)

    def test_empty_portfolio_and_unknown_api_cost(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            _create_database(paths.db_path, positions=False)
            snapshot = _source(paths).read()
            self.assertEqual(snapshot.positions, ())
            self.assertEqual(snapshot.portfolio.open_positions, 0)
            self.assertIsNone(snapshot.api.estimated_spend_today)
            self.assertIsNone(snapshot.api.remaining_budget)

    def test_positive_negative_and_resolved_positions(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            _create_database(paths.db_path, resolve_one=True)
            snapshot = _source(paths).read()
            by_id = {position.id: position for position in snapshot.positions}
            self.assertGreater(by_id[1].net_pnl or 0, 0)
            self.assertLess(by_id[2].net_pnl or 0, 0)
            self.assertEqual(by_id[3].status, "RESOLVED")
            self.assertEqual(by_id[3].quote_state, "RESOLVED")
            self.assertGreater(snapshot.portfolio.realized_pnl, 0)
            self.assertGreater(snapshot.api.unknown_historical_calls, 0)
            self.assertIsNone(snapshot.api.cost_per_evaluation)

    def test_external_runtime_path_resolution_from_unrelated_working_directory(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            paths = _paths(root)
            _create_database(paths.db_path, positions=False)
            env_file = root / "runtime.env"
            env_file.write_text(
                "BGL_RUNTIME_MODE=production\n"
                f"BGL_DB_PATH='{paths.db_path}'\n"
                f"BGL_LOG_DIR='{paths.log_dir}'\n"
                f"BGL_SIGNALS_DIR='{paths.signals_dir}'\n"
                f"BGL_REPORT_DIR='{paths.report_dir}'\n"
                f"BGL_EXPORT_DIR='{paths.export_dir}'\n"
                f"BGL_BACKUP_DIR='{paths.backup_dir}'\n"
                f"BGL_RUNTIME_DIR='{paths.runtime_dir}'\n"
                f"BGL_WATCHLIST_PATH='{paths.watchlist_path}'\n",
                encoding="utf-8",
            )
            resolved = get_runtime_paths({"BGL_RUNTIME_ENV_FILE": str(env_file)})
            self.assertEqual(resolved.db_path, paths.db_path.resolve())
            env = os.environ.copy()
            for key in (
                *PATH_ENV_VARS,
                "BGL_RUNTIME_MODE",
                "BGL_PRODUCTION_MODE",
                "SWARM_EDGE_ROOT",
            ):
                env.pop(key, None)
            env.update(
                {"BGL_RUNTIME_ENV_FILE": str(env_file), "PYTHONDONTWRITEBYTECODE": "1"}
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "operator_console.py"),
                    "revenue-status",
                ],
                cwd=root / "state",
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                result.returncode, 0, f"stdout={result.stdout}\nstderr={result.stderr}"
            )
            self.assertIn("REVENUE POC PORTFOLIO", result.stdout)
            self.assertFalse((root / "state" / "memory").exists())


class OperatorCliTests(unittest.TestCase):
    def test_commands_are_registered(self) -> None:
        self.assertEqual(
            COMMANDS,
            ("watch", "portfolio", "positions", "revenue-status", "discovery-breakdown"),
        )
        for command in COMMANDS:
            args = parser().parse_args(
                [command] + (["--snapshot"] if command == "watch" else [])
            )
            self.assertEqual(args.command, command)

    def test_one_shot_commands_use_supplied_read_only_source(self) -> None:
        source = mock.Mock()
        source.read.return_value = ConsoleSnapshot()
        for command in ("portfolio", "positions", "revenue-status", "discovery-breakdown", "watch"):
            argv = [command] + (["--snapshot"] if command == "watch" else [])
            with (
                self.subTest(command=command),
                mock.patch("operator_console.cli.render_command"),
            ):
                self.assertEqual(main(argv, source=source), 2)
        self.assertEqual(source.read.call_count, 5)

    def test_wrapper_registers_only_read_only_console_commands(self) -> None:
        wrapper = (ROOT / "bin" / "swarm-edge").read_text(encoding="utf-8")
        self.assertIn("watch|portfolio|positions|revenue-status|discovery-breakdown", wrapper)
        console_source = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (ROOT / "operator_console").glob("*.py")
        )
        for forbidden in (
            "from adapters",
            "import adapters",
            "RevenuePOCService",
            "submit_order",
            "place_order",
            "approve_trades",
            "launchctl kickstart",
            "launchctl kill",
            "apply_schema(",
        ):
            self.assertNotIn(forbidden, console_source)
        self.assertIn("mode=ro", console_source)
        self.assertIn("PRAGMA query_only=ON", console_source)


class StaticSource:
    def __init__(self, snapshot: ConsoleSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[bool] = []

    def read(self, *, include_logs: bool = True) -> ConsoleSnapshot:
        self.calls.append(include_logs)
        return self.snapshot


class OperatorAppTests(unittest.IsolatedAsyncioTestCase):
    async def test_small_terminal_layout_keyboard_navigation_and_detail(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            _create_database(paths.db_path, resolve_one=True)
            snapshot = _source(paths).read()
        source = StaticSource(snapshot)
        app = OperatorConsoleApp(source)  # type: ignore[arg-type]
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertTrue(app.has_class("compact"))
            await pilot.press("t")
            self.assertEqual(app.query_one("#tabs").active, "positions")
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, PositionDetailScreen)
            await pilot.press("escape")
            await pilot.press("p")
            self.assertEqual(app.query_one("#tabs").active, "portfolio")
            await pilot.press("a")
            self.assertEqual(app.query_one("#tabs").active, "api")
            await pilot.press("s")
            self.assertEqual(app.query_one("#tabs").active, "system")

    async def test_search_filters_positions_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            paths = _paths(Path(td))
            _create_database(paths.db_path)
            before = paths.db_path.read_bytes()
            snapshot = _source(paths).read()
            source = StaticSource(snapshot)
            app = OperatorConsoleApp(source)  # type: ignore[arg-type]
            async with app.run_test(size=(120, 35)) as pilot:
                await pilot.press("/")
                await pilot.press("m", "a", "r", "k", "e", "t", "-", "2", "enter")
                await pilot.pause()
                self.assertEqual(len(app.displayed_positions), 1)
                self.assertEqual(app.displayed_positions[0].market_id, "market-2")
            self.assertEqual(paths.db_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
