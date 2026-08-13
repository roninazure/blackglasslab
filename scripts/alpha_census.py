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
from census.collector import DEFAULT_DB, DEFAULT_PID, canonical_production_db, run_forever  # noqa: E402


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
        if not pid.exists(): print("stopped"); return 1
        try:
            value = int(pid.read_text().strip()); os.kill(value, 0)
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            print(f"stale pid file: {pid}"); return 1
        print(f"running pid={value} db={db}"); return 0
    if args.command == "stop":
        if pid.exists():
            try: os.kill(int(pid.read_text().strip()), signal.SIGTERM)
            except (ValueError, ProcessLookupError, PermissionError):
                try: pid.unlink()
                except FileNotFoundError: pass
                print("stale pid removed"); return 0
            print("stop requested"); return 0
        print("already stopped"); return 0
    if args.foreground: run_forever(db=db, pid=pid, interval=args.interval, limit=args.limit, duration_hours=args.duration_hours, log_path=db.with_suffix(".log")); return 0
    cmd = [sys.executable, str(Path(__file__).resolve()), "start", "--db", str(db), "--pid", str(pid), "--interval", str(args.interval), "--limit", str(args.limit), "--duration-hours", str(args.duration_hours), "--foreground"]
    subprocess.Popen(cmd, cwd=ROOT, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"started db={db} pid_file={pid}"); return 0


if __name__ == "__main__": raise SystemExit(main())
