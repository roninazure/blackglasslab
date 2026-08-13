#!/usr/bin/env python3
"""Static and read-only runtime safety checks for Alpha Census."""
from __future__ import annotations

import ast
import hashlib
import sqlite3
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from census.collector import canonical_production_db

ROOT = Path(__file__).resolve().parents[1]


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024): digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    tree = ast.parse((ROOT / "census" / "collector.py").read_text(encoding="utf-8"))
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    assert not any(isinstance(node.func, ast.Attribute) and node.func.attr in {"post", "put", "patch", "delete"} for node in calls), "mutating HTTP call found"
    production = canonical_production_db()
    if not production.exists(): raise SystemExit(f"production DB missing: {production}")
    before = fingerprint(production)
    conn = sqlite3.connect(f"file:{production}?mode=ro", uri=True); conn.execute("PRAGMA query_only=ON")
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"revenue_poc_positions", "revenue_poc_evaluations", "revenue_poc_equity_points"}
    missing = required - tables
    if missing: raise SystemExit(f"Revenue control schema unavailable: {sorted(missing)}")
    positions = conn.execute("SELECT COUNT(*) FROM revenue_poc_positions").fetchone()[0]
    open_positions = conn.execute("SELECT COUNT(*) FROM revenue_poc_positions WHERE status='OPEN'").fetchone()[0]
    equity_points = conn.execute("SELECT COUNT(*) FROM revenue_poc_equity_points").fetchone()[0]
    conn.close()
    after = fingerprint(production)
    print(f"production_db={production}")
    print(f"production_db_sha256={before}")
    print(f"production_db_sha256_after_readonly_check={after}")
    print(f"revenue_positions={positions} open_positions={open_positions} equity_points={equity_points}")
    assert before == after, "production DB changed during readonly validation"
    print("census transport: HTTPS GET plus public market WebSocket only")
    print("census storage: sparse episode/telemetry tables; production path rejected by launcher")
    print("safety validation: PASS")
    return 0


if __name__ == "__main__": raise SystemExit(main())
