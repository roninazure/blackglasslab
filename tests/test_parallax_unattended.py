from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from parallax_unattended import (
    HEALTH_FILENAME,
    SingletonAlreadyRunning,
    SingletonLock,
    UnattendedScheduler,
    atomic_write_json,
)


def test_second_singleton_fails_closed(tmp_path):
    first = SingletonLock(tmp_path / "runner.lock")
    second = SingletonLock(tmp_path / "runner.lock")
    first.acquire()
    try:
        with pytest.raises(SingletonAlreadyRunning):
            second.acquire()
    finally:
        first.close()


def test_atomic_health_replace_never_leaves_temporary_file(tmp_path):
    health = tmp_path / HEALTH_FILENAME
    atomic_write_json(health, {"state": "RUNNING", "generation": 1})
    atomic_write_json(health, {"state": "DEGRADED", "generation": 2})
    assert json.loads(health.read_text()) == {"generation": 2, "state": "DEGRADED"}
    assert not list(tmp_path.glob(f".{HEALTH_FILENAME}.*"))


def test_startup_runs_active_nfl_mlb_and_reconciliation_immediately(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00",
    )
    health = scheduler.run_cycle()

    assert [command for command, _ in calls] == [
        [sys.executable, str((tmp_path / "release/scripts/nfl_live_scan.py").resolve())],
        [sys.executable, "-m", "parallax", "scan", "--limit", "6"],
        [sys.executable, str((tmp_path / "release/scripts/parallax_reconcile_prospective.py").resolve())],
    ]
    assert health["state"] == "RUNNING"
    assert health["last_successful_nfl_scan"] is not None
    assert health["last_successful_cfb_scan"] is None
    assert health["last_successful_mlb_scan"] is not None
    assert health["last_successful_reconciliation"] is not None
    assert json.loads((tmp_path / "state" / HEALTH_FILENAME).read_text()) == health


def test_active_sports_run_on_startup_then_hourly_while_reconciliation_runs_each_cycle(tmp_path):
    calls = []
    now = [0.0]

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00",
        monotonic=lambda: now[0],
    )
    scheduler.run_cycle()
    assert len(calls) == 3

    now[0] = 300
    scheduler.run_cycle()
    assert len(calls) == 4

    now[0] = 3600
    health = scheduler.run_cycle()
    assert len(calls) == 7
    assert health["last_successful_nfl_scan"] is not None
    assert health["last_successful_cfb_scan"] is None
    assert health["last_successful_mlb_scan"] is not None


def test_cfb_requires_explicit_enable_and_isolated_failure(tmp_path):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("PARALLAX_CFB_ENABLED=1\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        if command[1].endswith("cfb_live_scan.py"):
            return subprocess.CompletedProcess(command, 1, "", "429")
        return subprocess.CompletedProcess(command, 0, "{}", "")

    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runtime_env=runtime_env,
        runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00",
        monotonic=lambda: 0.0,
    )
    health = scheduler.run_cycle()

    assert len(calls) == 4
    assert health["last_successful_nfl_scan"] is not None
    assert health["last_successful_cfb_scan"] is None
    assert health["last_successful_mlb_scan"] is not None
    assert health["state"] == "DEGRADED"


def test_all_enabled_lane_failures_write_failed_health(tmp_path):
    runtime_env = tmp_path / "runtime.env"
    runtime_env.write_text("PARALLAX_CFB_ENABLED=1\n", encoding="utf-8")

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 600)

    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runtime_env=runtime_env,
        runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00",
        monotonic=lambda: 0.0,
    )
    health = scheduler.run_cycle()
    assert health["state"] == "FAILED"
    assert len(health["recent_errors"]) == 4


def test_successful_nfl_and_mlb_export_once_other_lanes_never_export(
    tmp_path, monkeypatch
):
    now = [3600.0]
    runner_calls = []
    export_calls = []

    def runner(command, **kwargs):
        runner_calls.append(command)
        return subprocess.CompletedProcess(command, 0, '{"ok": true}', "")

    def exporter(lane, stdout, state_dir):
        export_calls.append((lane, stdout, state_dir))

    monkeypatch.setattr("parallax_unattended.export_completed_scan", exporter)
    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        monotonic=lambda: now[0],
        clock=lambda: "2026-09-24T00:00:00+00:00",
    )
    scheduler.next_due = {lane: 0.0 for lane in ("nfl", "cfb", "mlb")}

    health = scheduler.run_cycle()

    assert len(runner_calls) == 3
    assert [call[0] for call in export_calls] == ["nfl", "mlb"]
    assert all(call[1] == '{"ok": true}' for call in export_calls)
    assert all(call[2] == (tmp_path / "state").resolve() for call in export_calls)
    assert health["state"] == "RUNNING"


def test_failed_lane_never_exports_and_runner_count_is_unchanged(tmp_path, monkeypatch):
    runner_calls = []
    export_calls = []

    def runner(command, **kwargs):
        runner_calls.append(command)
        lane_failed = command[1].endswith("nfl_live_scan.py")
        return subprocess.CompletedProcess(command, 1 if lane_failed else 0, "{}", "failed")

    monkeypatch.setattr(
        "parallax_unattended.export_completed_scan",
        lambda lane, stdout, state_dir: export_calls.append(lane),
    )
    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        monotonic=lambda: 3600.0,
        clock=lambda: "2026-09-24T00:00:00+00:00",
    )
    scheduler.next_due = {lane: 0.0 for lane in ("nfl", "cfb", "mlb")}

    health = scheduler.run_cycle()

    assert len(runner_calls) == 3
    assert export_calls == ["mlb"]
    assert health["last_successful_nfl_scan"] is None
    assert health["last_successful_mlb_scan"] is not None
    assert health["state"] == "DEGRADED"


def test_exporter_exception_preserves_successful_scan_and_later_lanes(
    tmp_path, monkeypatch
):
    runner_calls = []
    export_calls = []

    def runner(command, **kwargs):
        runner_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "{}", "")

    def exporter(lane, stdout, state_dir):
        export_calls.append(lane)
        if lane == "nfl":
            raise OSError("local export unavailable")

    monkeypatch.setattr("parallax_unattended.export_completed_scan", exporter)
    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        monotonic=lambda: 3600.0,
        clock=lambda: "2026-09-24T00:00:00+00:00",
    )
    scheduler.next_due = {lane: 0.0 for lane in ("nfl", "cfb", "mlb")}

    health = scheduler.run_cycle()

    assert len(runner_calls) == 3
    assert export_calls == ["nfl", "mlb"]
    assert health["last_successful_nfl_scan"] is not None
    assert health["last_successful_mlb_scan"] is not None
    assert health["state"] == "RUNNING"
    assert health["recent_errors"] == []
