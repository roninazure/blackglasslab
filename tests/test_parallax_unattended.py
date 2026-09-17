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


def test_cycle_invokes_all_existing_lanes_and_isolates_failure(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        if command[1].endswith("nfl_live_scan.py"):
            return subprocess.CompletedProcess(command, 1, "", "official source unavailable")
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
        [sys.executable, str((tmp_path / "release/scripts/cfb_live_scan.py").resolve())],
        [sys.executable, "-m", "parallax", "scan", "--limit", "6"],
        [sys.executable, str((tmp_path / "release/scripts/parallax_reconcile_prospective.py").resolve())],
    ]
    assert health["state"] == "DEGRADED"
    assert health["last_successful_nfl_scan"] is None
    assert health["last_successful_cfb_scan"] is not None
    assert health["last_successful_mlb_scan"] is not None
    assert health["last_successful_reconciliation"] is not None
    assert "nfl:" in health["recent_errors"][0]
    assert json.loads((tmp_path / "state" / HEALTH_FILENAME).read_text()) == health


def test_all_lane_failures_write_failed_health(tmp_path):
    def runner(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 600)

    scheduler = UnattendedScheduler(
        root=tmp_path / "release",
        state_dir=tmp_path / "state",
        runner=runner,
        clock=lambda: "2026-09-17T00:00:00+00:00",
    )
    health = scheduler.run_cycle()
    assert health["state"] == "FAILED"
    assert len(health["recent_errors"]) == 4
