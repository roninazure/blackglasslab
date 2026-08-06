#!/usr/bin/env python3
"""Print the latest persisted Polymarket discovery funnel, read-only."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reporting.discovery_breakdown import (
    build_discovery_breakdown,
    render_discovery_breakdown,
)
from swarm_edge_runtime import RUNTIME_PATHS


def main() -> int:
    report_path = RUNTIME_PATHS.signals_dir / "infer_pipeline_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    uri = f"file:{RUNTIME_PATHS.db_path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        data = build_discovery_breakdown(report, conn=conn)
    finally:
        conn.close()
    print(render_discovery_breakdown(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
