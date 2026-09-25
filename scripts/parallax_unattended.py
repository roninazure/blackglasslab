#!/usr/bin/env python3
"""Run the existing PARALLAX sports scans and reconciliation as one owner.

This is deliberately a thin process supervisor.  It owns scheduling, a local
advisory lock, and an operator-readable health file; each sports lane retains
its existing command and business logic.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from parallax.public_feed import export_completed_scan


UTC = timezone.utc
HEALTH_FILENAME = "parallax_unattended_health.json"
LOCK_FILENAME = "parallax_unattended.lock"
LANES = ("nfl", "cfb", "mlb", "reconciliation")
SPORTS_LANES = ("nfl", "cfb", "mlb")
SPORTS_INTERVAL_SECONDS = 3600


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class SingletonAlreadyRunning(RuntimeError):
    """Raised when another unattended owner holds the local lock."""


class SingletonLock:
    """Non-blocking advisory file lock, released automatically on process exit."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise SingletonAlreadyRunning(f"another runner holds {self.path}") from exc

    def close(self) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def atomic_write_json(path: Path, payload: Mapping[str, object]) -> None:
    """Replace *path* atomically so phone/status readers never see partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def load_runtime_env(path: Path | None) -> dict[str, str]:
    """Read the existing KEY=VALUE runtime file without evaluating shell code."""
    if path is None:
        return {}
    result: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.isidentifier():
            result[key] = value.strip().strip("\"'")
    return result


def release_sha(root: Path) -> str:
    configured = os.environ.get("PARALLAX_RELEASE_SHA")
    if configured:
        return configured
    try:
        return subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        candidate = root.resolve().name
        return candidate if len(candidate) == 40 else "unknown"


class UnattendedScheduler:
    def __init__(
        self,
        *,
        root: Path,
        state_dir: Path,
        runtime_env: Path | None = None,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        clock: Callable[[], str] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        timeout_seconds: int = 600,
    ) -> None:
        self.root = root.resolve()
        self.state_dir = state_dir.resolve()
        self.runtime_env = runtime_env
        self.runner = runner
        self.clock = clock
        self.monotonic = monotonic
        self.timeout_seconds = timeout_seconds
        self.started_at = clock()
        self.last_success: dict[str, str | None] = {lane: None for lane in LANES}
        startup = self.monotonic()
        self.next_due: dict[str, float] = {
            lane: startup for lane in SPORTS_LANES
        }
        self.recent_errors: list[str] = []

    @property
    def health_path(self) -> Path:
        return self.state_dir / HEALTH_FILENAME

    @property
    def lock_path(self) -> Path:
        return self.state_dir / LOCK_FILENAME

    def _cfb_enabled(self) -> bool:
        value = self._environment().get("PARALLAX_CFB_ENABLED", "").strip().casefold()
        return value in {"1", "true", "yes", "on"}

    def commands(self) -> dict[str, list[str]]:
        commands = {
            "nfl": [sys.executable, str(self.root / "scripts/nfl_live_scan.py")],
        }
        if self._cfb_enabled():
            commands["cfb"] = [sys.executable, str(self.root / "scripts/cfb_live_scan.py")]
        commands["mlb"] = [sys.executable, "-m", "parallax", "scan", "--limit", "6"]
        commands["reconciliation"] = [
            sys.executable,
            str(self.root / "scripts/parallax_reconcile_prospective.py"),
        ]
        return commands

    def _environment(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(load_runtime_env(self.runtime_env))
        previous = env.get("PYTHONPATH")
        env["PYTHONPATH"] = str(self.root) if not previous else f"{self.root}{os.pathsep}{previous}"
        return env

    def _health(self, state: str, heartbeat: str) -> dict[str, object]:
        return {
            "state": state,
            "release_sha": release_sha(self.root),
            "runner_started_at": self.started_at,
            "last_heartbeat": heartbeat,
            "last_successful_nfl_scan": self.last_success["nfl"],
            "last_successful_cfb_scan": self.last_success["cfb"],
            "last_successful_mlb_scan": self.last_success["mlb"],
            "last_successful_reconciliation": self.last_success["reconciliation"],
            "recent_errors": self.recent_errors[-8:],
        }

    def run_cycle(self) -> dict[str, object]:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        successful = 0
        errors: list[str] = []
        now = self.monotonic()
        commands = self.commands()
        for lane, command in commands.items():
            if lane in SPORTS_LANES and now < self.next_due[lane]:
                continue
            if lane in SPORTS_LANES:
                self.next_due[lane] = now + SPORTS_INTERVAL_SECONDS
            try:
                completed = self.runner(
                    command,
                    cwd=self.state_dir,
                    env=self._environment(),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout_seconds,
                    check=False,
                )
                if completed.returncode != 0:
                    detail = (completed.stderr or completed.stdout or "no output").strip().replace("\n", " ")
                    raise RuntimeError(f"exit {completed.returncode}: {detail[:400]}")
                self.last_success[lane] = self.clock()
                successful += 1
                if completed.stdout:
                    print(f"[{lane}] {completed.stdout.strip()}", flush=True)
                if lane in {"nfl", "mlb"}:
                    try:
                        export_completed_scan(lane, completed.stdout, self.state_dir)
                    except Exception as exc:  # Export must not change scan success.
                        print(
                            f"[WARN] {lane} public feed export failed: {type(exc).__name__}",
                            file=sys.stderr,
                            flush=True,
                        )
            except Exception as exc:  # Each lane must not prevent later lanes.
                message = f"{lane}: {type(exc).__name__}: {exc}"
                errors.append(message)
                print(f"[WARN] {message}", file=sys.stderr, flush=True)

        self.recent_errors.extend(errors)
        heartbeat = self.clock()
        state = "RUNNING" if not errors else ("FAILED" if successful == 0 else "DEGRADED")
        health = self._health(state, heartbeat)
        atomic_write_json(self.health_path, health)
        print(f"[health] state={state} successful_lanes={successful}/{len(commands)}", flush=True)
        return health

    def run_forever(self, interval_seconds: int) -> None:
        lock = SingletonLock(self.lock_path)
        lock.acquire()
        try:
            while True:
                self.run_cycle()
                time.sleep(interval_seconds)
        finally:
            lock.close()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--runtime-env", type=Path)
    parser.add_argument("--interval-seconds", type=int, default=300)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if args.interval_seconds < 1 or args.timeout_seconds < 1:
        parser.error("interval and timeout must be positive")
    scheduler = UnattendedScheduler(
        root=args.root,
        state_dir=args.state_dir,
        runtime_env=args.runtime_env,
        timeout_seconds=args.timeout_seconds,
    )
    try:
        scheduler.run_forever(args.interval_seconds)
    except SingletonAlreadyRunning as exc:
        print(f"[FATAL] {exc}", file=sys.stderr, flush=True)
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
