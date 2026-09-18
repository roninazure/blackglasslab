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


def test_startup_runs_reconciliation_only_and_preserves_health(tmp_path):
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

    assert [command for command, _ in calls] == [[
        sys.executable, str((tmp_path / "release/scripts/parallax_reconcile_prospective.py").resolve())
    ]]
    assert health["state"] == "RUNNING"
    assert health["last_successful_nfl_scan"] is None
    assert health["last_successful_cfb_scan"] is None
    assert health["last_successful_mlb_scan"] is None
    assert health["last_successful_reconciliation"] is not None
    assert json.loads((tmp_path / "state" / HEALTH_FILENAME).read_text()) == health


def test_due_sports_attempts_reset_after_failure_and_skip_heartbeats(tmp_path):
    calls = []
    now = [0.0]

    def runner(command, **kwargs):
        calls.append(command)
        if command[1].endswith("cfb_live_scan.py"):
            return subprocess.CompletedProcess(command, 1, "", "429")
        return subprocess.CompletedProcess(command, 0, "", "")

    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00", monotonic=lambda: now[0],
    )
    scheduler.run_cycle()
    assert len(calls) == 1
    now[0] = 300
    scheduler.run_cycle()
    assert len(calls) == 2
    now[0] = 3600
    health = scheduler.run_cycle()
    assert len(calls) == 6
    assert health["last_successful_nfl_scan"] is not None
    assert health["last_successful_cfb_scan"] is None
    assert health["last_successful_mlb_scan"] is not None
    now[0] = 3900
    scheduler.run_cycle()
    assert len(calls) == 7


def test_all_lane_failures_write_failed_health_when_due(tmp_path):
    now = [0.0]

    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 600)

    scheduler = UnattendedScheduler(
        root=tmp_path / "release", state_dir=tmp_path / "state", runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00", monotonic=lambda: now[0],
    )
    now[0] = 3600
    health = scheduler.run_cycle()
    assert health["state"] == "FAILED"
    assert len(health["recent_errors"]) == 4
