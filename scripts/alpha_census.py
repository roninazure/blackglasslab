#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from census.collector import (  # noqa: E402
    DEFAULT_DB,
    DEFAULT_PID,
    canonical_production_db,
    read_collector_status,
    run_forever,
)


def _paths(args: argparse.Namespace) -> tuple[Path, Path]:
    db = Path(args.db).resolve() if args.db else DEFAULT_DB.resolve()
    pid = Path(args.pid).resolve() if args.pid else DEFAULT_PID.resolve()
    production = canonical_production_db()
    if db == production: raise SystemExit("refusing to use the production DB as census storage")
    return db, pid


def main() -> int:
    parser = argparse.ArgumentParser(description="Swarm Edge read-only Alpha Census")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "stop"):
        p = sub.add_parser(name); p.add_argument("--db"); p.add_argument("--pid")
        if name == "start": p.add_argument("--interval", type=float, default=60.0, help="bootstrap/recovery interval only"); p.add_argument("--limit", type=int, default=100); p.add_argument("--duration-hours", type=float); p.add_argument("--foreground", action="store_true")
    args = parser.parse_args(); db, pid = _paths(args)
    if args.command == "status":
        state = read_collector_status(db)
        if not pid.exists():
            if state:
                error = f" error={state['error']}" if state.get("error") else ""
                print(f"{state['status']} phase={state['phase']} updated_at_utc={state['updated_at_utc']} db={db}{error}")
                return 2 if state["status"] == "FAILED" else 1
            print("STOPPED"); return 1
        try:
            value = int(pid.read_text().strip()); os.kill(value, 0)
        except PermissionError:
            pass  # The process exists, but this caller cannot signal it.
        except (ValueError, ProcessLookupError, OSError):
            print(f"FAILED stale pid file: {pid}"); return 2
        if not state:
            print(f"STARTING phase=initializing pid={value} db={db}"); return 0
        error = f" error={state['error']}" if state.get("error") else ""
        print(f"{state['status']} phase={state['phase']} updated_at_utc={state['updated_at_utc']} pid={value} db={db}{error}")
        return 2 if state["status"] == "FAILED" else 0
    if args.command == "stop":
        if pid.exists():
            try: os.kill(int(pid.read_text().strip()), signal.SIGTERM)
            except PermissionError:
                print(f"permission denied signaling census process from {pid}"); return 2
            except (ValueError, ProcessLookupError):
                try: pid.unlink()
                except FileNotFoundError: pass
                print("stale pid removed"); return 0
            print("stop requested"); return 0
        print("already stopped"); return 0
    if args.foreground: run_forever(db=db, pid=pid, interval=args.interval, limit=args.limit, duration_hours=args.duration_hours, log_path=db.with_suffix(".log")); return 0
    cmd = [sys.executable, str(Path(__file__).resolve()), "start", "--db", str(db), "--pid", str(pid), "--interval", str(args.interval), "--limit", str(args.limit), "--foreground"]
    if args.duration_hours is not None:
        cmd.extend(["--duration-hours", str(args.duration_hours)])
    subprocess.Popen(cmd, cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"started db={db} pid_file={pid}"); return 0


if __name__ == "__main__": raise SystemExit(main())
