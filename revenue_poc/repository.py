from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
UPGRADE = ROOT / "migrations" / "006_revenue_poc_v1.sql"
DOWNGRADE = ROOT / "migrations" / "006_revenue_poc_v1_down.sql"
UPGRADE_V11 = ROOT / "migrations" / "007_revenue_poc_v1_1.sql"
DOWNGRADE_V11 = ROOT / "migrations" / "007_revenue_poc_v1_1_down.sql"


def apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(UPGRADE.read_text(encoding="utf-8"))
    # The v1.1 migration is intentionally idempotent for copied production
    # databases.  Existing v1 databases receive the optimization telemetry;
    # fresh test databases receive both migrations in one call.
    columns = {
        str(row[1])
        for row in conn.execute("PRAGMA table_info(revenue_poc_api_calls)")
    }
    if "routing_tier" not in columns:
        conn.executescript(UPGRADE_V11.read_text(encoding="utf-8"))
    conn.commit()


def downgrade_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(DOWNGRADE_V11.read_text(encoding="utf-8"))
    conn.executescript(DOWNGRADE.read_text(encoding="utf-8"))
    conn.commit()


def table_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
