#!/usr/bin/env python3
"""Provision and validate a non-live Swarm Edge migration rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tarfile
import tempfile
from pathlib import Path


STATE_DIRS = ("watchlists", "signals", "exports", "reports")


def secure_mkdir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)


def provision(target: Path, release_sha: str) -> dict[str, str]:
    paths = {
        "root": target,
        "bin": target / "bin",
        "release": target / "releases" / release_sha,
        "venv": target / "venvs" / release_sha,
        "config": target / "config",
        "state": target / "state",
        "run": target / "run",
        "logs": target.parent.parent / "Logs" / "SwarmEdge",
        "backups": target.parent.parent / "Application Support" / "SwarmEdgeBackups",
    }
    for path in paths.values():
        secure_mkdir(path)
    for name in STATE_DIRS:
        secure_mkdir(paths["state"] / name)
    return {key: str(value) for key, value in paths.items()}


def create_release(source: Path, target_release: Path, sha: str) -> None:
    target_release.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        archive = Path(td) / "release.tar"
        with archive.open("wb") as handle:
            subprocess.run(
                ["git", "-C", str(source), "archive", "--format=tar", sha],
                stdout=handle,
                check=True,
            )
        target_release.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive) as tar:
            tar.extractall(target_release)


def render_plist(template: Path, output: Path, *, root: Path, log_dir: Path, bin_path: Path) -> None:
    text = template.read_text(encoding="utf-8")
    replacements = {
        "@SWARM_EDGE_ROOT@": str(root),
        "@SWARM_EDGE_LOG_DIR@": str(log_dir),
        "@SWARM_EDGE_BIN@": str(bin_path),
    }
    for key, value in replacements.items():
        text = text.replace(key, value)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(text, encoding="utf-8")
    output.chmod(0o600)
    subprocess.run(["plutil", "-lint", str(output)], check=True, capture_output=True, text=True)


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    destination_conn = sqlite3.connect(destination)
    try:
        source_conn.backup(destination_conn)
        destination_conn.commit()
    finally:
        destination_conn.close()
        source_conn.close()
    destination.chmod(0o600)


def inspect_db(path: Path, watchlist: Path | None = None) -> dict[str, object]:
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    quick_check = conn.execute("PRAGMA quick_check").fetchone()[0]
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    paper_counts = dict(conn.execute("SELECT status, COUNT(*) FROM paper_trades GROUP BY status")) if "paper_trades" in tables else {}
    shadow_count = conn.execute("SELECT COUNT(*) FROM shadow_forecasts").fetchone()[0] if "shadow_forecasts" in tables else 0
    cursor_row = conn.execute("SELECT value FROM kv WHERE key='infer_cursor'").fetchone() if "kv" in tables else None
    conn.close()
    result: dict[str, object] = {
        "quick_check": quick_check,
        "tables": tables,
        "paper_counts": paper_counts,
        "shadow_forecasts": shadow_count,
        "infer_cursor": cursor_row[0] if cursor_row else None,
    }
    if watchlist and watchlist.exists():
        result["watchlist_sha256"] = hashlib.sha256(watchlist.read_bytes()).hexdigest()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("provision")
    p.add_argument("--target", required=True, type=Path)
    p.add_argument("--sha", required=True)
    p = sub.add_parser("backup")
    p.add_argument("--source-db", required=True, type=Path)
    p.add_argument("--destination", required=True, type=Path)
    p = sub.add_parser("inspect")
    p.add_argument("--db", required=True, type=Path)
    p.add_argument("--watchlist", type=Path)
    args = parser.parse_args()
    if args.command == "provision":
        print(json.dumps(provision(args.target.expanduser(), args.sha), sort_keys=True, indent=2))
    elif args.command == "backup":
        sqlite_backup(args.source_db.expanduser(), args.destination.expanduser())
        print(json.dumps(inspect_db(args.destination.expanduser()), sort_keys=True, indent=2))
    else:
        print(json.dumps(inspect_db(args.db.expanduser(), args.watchlist), sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
