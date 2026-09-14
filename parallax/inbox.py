from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .models import AttentionClass, InboxStatus, utcnow

INBOX_ACTIVE_LIMIT = 10
INBOX_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "SwarmEdge"
    / "state"
    / "parallax_inbox.sqlite"
)
ATTENTION_PRIORITY = {
    AttentionClass.ACTIONABLE_PLAY: 0,
    AttentionClass.PUBLIC_WORTHY: 1,
    AttentionClass.PRIORITY_WATCH: 2,
}


@dataclass(frozen=True)
class InboxUpsert:
    attention_class: AttentionClass
    source_id: str
    venue: str
    market_id: str
    display_title: str
    headline: str
    verdict: str
    actionability: str
    directional_read: str
    trade_confidence: str
    market_activity_strength: str
    what_happened: str
    what_it_means: str
    operator_instruction: str
    detected_at: str
    expires_at: str | None
    payload: dict[str, Any]


def default_inbox_store() -> InboxStore:
    return InboxStore(INBOX_PATH)


class InboxStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS inbox_items (
                    inbox_id TEXT PRIMARY KEY,
                    attention_class TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    display_title TEXT NOT NULL,
                    headline TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    actionability TEXT NOT NULL,
                    directional_read TEXT NOT NULL,
                    trade_confidence TEXT NOT NULL,
                    market_activity_strength TEXT NOT NULL,
                    what_happened TEXT NOT NULL,
                    what_it_means TEXT NOT NULL,
                    operator_instruction TEXT NOT NULL,
                    detected_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    seen_at TEXT,
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS inbox_items_status_priority
                ON inbox_items(status, attention_class, updated_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS inbox_items_market_status
                ON inbox_items(venue, market_id, status)
                """
            )

    @staticmethod
    def inbox_id(venue: str, market_id: str, attention_class: AttentionClass) -> str:
        return f"inbox-{venue}-{market_id}-{attention_class.value}"

    def upsert(self, item: InboxUpsert) -> None:
        now = utcnow().isoformat()
        inbox_id = self.inbox_id(item.venue, item.market_id, item.attention_class)
        payload_json = json.dumps(item.payload, allow_nan=False, sort_keys=True)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE inbox_items
                SET status = ?
                WHERE venue = ?
                    AND market_id = ?
                    AND status = ?
                    AND attention_class != ?
                """,
                (
                    InboxStatus.EXPIRED.value,
                    item.venue,
                    item.market_id,
                    InboxStatus.ACTIVE.value,
                    item.attention_class.value,
                ),
            )
            conn.execute(
                """
                INSERT INTO inbox_items (
                    inbox_id, attention_class, source_id, venue, market_id,
                    display_title, headline, verdict, actionability,
                    directional_read, trade_confidence, market_activity_strength,
                    what_happened, what_it_means, operator_instruction,
                    detected_at, created_at, updated_at, expires_at, seen_at,
                    status, payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(inbox_id) DO UPDATE SET
                    source_id = excluded.source_id,
                    display_title = excluded.display_title,
                    headline = excluded.headline,
                    verdict = excluded.verdict,
                    actionability = excluded.actionability,
                    directional_read = excluded.directional_read,
                    trade_confidence = excluded.trade_confidence,
                    market_activity_strength = excluded.market_activity_strength,
                    what_happened = excluded.what_happened,
                    what_it_means = excluded.what_it_means,
                    operator_instruction = excluded.operator_instruction,
                    detected_at = excluded.detected_at,
                    updated_at = excluded.updated_at,
                    expires_at = excluded.expires_at,
                    status = excluded.status,
                    payload_json = excluded.payload_json
                """,
                (
                    inbox_id,
                    item.attention_class.value,
                    item.source_id,
                    item.venue,
                    item.market_id,
                    item.display_title,
                    item.headline,
                    item.verdict,
                    item.actionability,
                    item.directional_read,
                    item.trade_confidence,
                    item.market_activity_strength,
                    item.what_happened,
                    item.what_it_means,
                    item.operator_instruction,
                    item.detected_at,
                    now,
                    now,
                    item.expires_at,
                    InboxStatus.ACTIVE.value,
                    payload_json,
                ),
            )

    def expire_missing_active(self, active_ids: set[str]) -> None:
        with self._connect() as conn:
            if not active_ids:
                conn.execute(
                    "UPDATE inbox_items SET status = ? WHERE status = ?",
                    (InboxStatus.EXPIRED.value, InboxStatus.ACTIVE.value),
                )
                return
            placeholders = ",".join("?" for _ in active_ids)
            conn.execute(
                f"""
                UPDATE inbox_items
                SET status = ?
                WHERE status = ?
                    AND inbox_id NOT IN ({placeholders})
                """,
                (InboxStatus.EXPIRED.value, InboxStatus.ACTIVE.value, *active_ids),
            )

    def mark_seen(self, inbox_id: str) -> dict[str, Any]:
        seen_at = utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT seen_at FROM inbox_items WHERE inbox_id = ?",
                (inbox_id,),
            ).fetchone()
            if row is None:
                raise KeyError(inbox_id)
            if row["seen_at"] is None:
                conn.execute(
                    "UPDATE inbox_items SET seen_at = ?, updated_at = ? WHERE inbox_id = ?",
                    (seen_at, seen_at, inbox_id),
                )
        found = self.get(inbox_id)
        if found is None:
            raise KeyError(inbox_id)
        return found

    def get(self, inbox_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM inbox_items WHERE inbox_id = ?",
                (inbox_id,),
            ).fetchone()
        return None if row is None else self._row_to_item(row)

    def items(self, *, include_expired: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM inbox_items"
        args: tuple[str, ...] = ()
        if not include_expired:
            query += " WHERE status = ?"
            args = (InboxStatus.ACTIVE.value,)
        with self._connect() as conn:
            rows = conn.execute(query, args).fetchall()
        active = [row for row in rows if row["status"] == InboxStatus.ACTIVE.value]
        expired = [row for row in rows if row["status"] != InboxStatus.ACTIVE.value]
        active = sorted(
            active,
            key=lambda row: (
                ATTENTION_PRIORITY[AttentionClass(row["attention_class"])],
                -_sortable_time(row["updated_at"]),
                -_sortable_time(row["detected_at"]),
            ),
        )[:INBOX_ACTIVE_LIMIT]
        expired = sorted(
            expired,
            key=lambda row: (
                ATTENTION_PRIORITY[AttentionClass(row["attention_class"])],
                -_sortable_time(row["updated_at"]),
                -_sortable_time(row["detected_at"]),
            ),
        )
        ordered = active + expired if include_expired else active
        return [self._row_to_item(row) for row in ordered]

    def _row_to_item(self, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        payload.update(
            {
                "inbox_id": row["inbox_id"],
                "attention_class": row["attention_class"],
                "source_id": row["source_id"],
                "venue": row["venue"],
                "market_id": row["market_id"],
                "display_title": row["display_title"],
                "market": row["display_title"],
                "headline": row["headline"],
                "verdict": row["verdict"],
                "actionability": row["actionability"],
                "directional_read": row["directional_read"],
                "trade_confidence": row["trade_confidence"],
                "market_activity_strength": row["market_activity_strength"],
                "what_happened": row["what_happened"],
                "what_it_means": row["what_it_means"],
                "operator_instruction": row["operator_instruction"],
                "detected_at": row["detected_at"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "expires_at": row["expires_at"],
                "seen": row["seen_at"] is not None,
                "seen_at": row["seen_at"],
                "status": row["status"],
            }
        )
        return payload


def _sortable_time(value: str) -> float:
    try:
        from datetime import datetime

        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0
