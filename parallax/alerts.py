from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .models import Action, AttentionClass, NormalizedMarket, ParallaxPlay, timestamp, utcnow

logger = logging.getLogger(__name__)

ALERT_CONFIG_PATH = (
    Path.home()
    / "Library"
    / "Application Support"
    / "SwarmEdge"
    / "parallax-alerts.env"
)
ALERT_PRIORITIES = {
    AttentionClass.ACTIONABLE_PLAY.value: "CRITICAL",
    AttentionClass.PUBLIC_WORTHY.value: "HIGH",
    AttentionClass.PRIORITY_WATCH.value: "NORMAL",
}
NTFY_PRIORITIES = {
    "CRITICAL": 5,
    "HIGH": 4,
    "NORMAL": 3,
}
ALERT_TITLES = {
    AttentionClass.ACTIONABLE_PLAY.value: "PARALLAX ACTIONABLE PLAY",
    AttentionClass.PUBLIC_WORTHY.value: "PARALLAX PUBLIC-WORTHY",
    AttentionClass.PRIORITY_WATCH.value: "PARALLAX PRIORITY WATCH",
}
DEFAULT_NTFY_SERVER = "https://ntfy.sh"
MAX_ATTEMPTS = 2
WEBHOOK_TIMEOUT_SECONDS = 5
MAX_RESPONSE_BYTES = 4096


def _pytest_active() -> bool:
    return bool(os.environ.get("PYTEST_CURRENT_TEST")) or "pytest" in sys.modules


@dataclass(frozen=True)
class AlertConfig:
    mode: str = "disabled"
    webhook_url: str | None = None
    ntfy_topic: str | None = None
    ntfy_server: str = DEFAULT_NTFY_SERVER

    @classmethod
    def load(cls, path: Path = ALERT_CONFIG_PATH) -> AlertConfig:
        values: dict[str, str] = {}
        if path.exists():
            try:
                stat = path.stat()
                if stat.st_mode & 0o077 == 0:
                    values.update(_parse_env_file(path))
            except OSError:
                pass
        values.update(
            {
                key: value
                for key, value in os.environ.items()
                if key
                in {
                    "PARALLAX_ALERT_MODE",
                    "PARALLAX_ALERT_WEBHOOK_URL",
                    "PARALLAX_ALERT_NTFY_TOPIC",
                    "PARALLAX_ALERT_NTFY_SERVER",
                }
            }
        )
        mode = values.get("PARALLAX_ALERT_MODE", "disabled").strip().casefold()
        if mode not in {"disabled", "dry_run", "webhook", "ntfy"}:
            mode = "disabled"
        webhook_url = values.get("PARALLAX_ALERT_WEBHOOK_URL")
        ntfy_topic = values.get("PARALLAX_ALERT_NTFY_TOPIC")
        ntfy_server = values.get("PARALLAX_ALERT_NTFY_SERVER", DEFAULT_NTFY_SERVER)
        return cls(
            mode=mode,
            webhook_url=webhook_url.strip() if webhook_url else None,
            ntfy_topic=ntfy_topic.strip() if ntfy_topic else None,
            ntfy_server=ntfy_server.strip() or DEFAULT_NTFY_SERVER,
        )


@dataclass(frozen=True)
class DeliveryResult:
    status: str
    http_status: int | None = None
    error_code: str | None = None
    error_summary: str | None = None


class AlertTransport(Protocol):
    def post_json(self, url: str, payload: dict[str, Any]) -> DeliveryResult:
        ...


class WebhookTransport:
    def post_json(self, url: str, payload: dict[str, Any]) -> DeliveryResult:
        if not url.startswith("https://"):
            return DeliveryResult(
                "FAILED",
                error_code="INVALID_WEBHOOK_URL",
                error_summary="Webhook URL must use HTTPS.",
            )
        body = json.dumps(payload, allow_nan=False).encode()
        request = Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=WEBHOOK_TIMEOUT_SECONDS) as response:
                response.read(MAX_RESPONSE_BYTES)
                status = int(response.status)
        except HTTPError as exc:
            exc.read(MAX_RESPONSE_BYTES)
            if 500 <= exc.code <= 599:
                return DeliveryResult(
                    "FAILED",
                    http_status=exc.code,
                    error_code="HTTP_5XX",
                    error_summary=f"Webhook returned HTTP {exc.code}.",
                )
            return DeliveryResult(
                "FAILED",
                http_status=exc.code,
                error_code="HTTP_4XX",
                error_summary=f"Webhook returned HTTP {exc.code}.",
            )
        except TimeoutError:
            return DeliveryResult(
                "FAILED",
                error_code="TIMEOUT",
                error_summary="Webhook request timed out before a response.",
            )
        except URLError as exc:
            return DeliveryResult(
                "UNKNOWN",
                error_code="NETWORK_UNKNOWN",
                error_summary=type(exc.reason).__name__
                if hasattr(exc, "reason")
                else "Network outcome is unknown.",
            )
        if 200 <= status <= 299:
            return DeliveryResult("SENT", http_status=status)
        if 500 <= status <= 599:
            return DeliveryResult(
                "FAILED",
                http_status=status,
                error_code="HTTP_5XX",
                error_summary=f"Webhook returned HTTP {status}.",
            )
        return DeliveryResult(
            "FAILED",
            http_status=status,
            error_code="HTTP_4XX",
            error_summary=f"Webhook returned HTTP {status}.",
        )


class AlertDeliveryStore:
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
                CREATE TABLE IF NOT EXISTS alert_deliveries (
                    delivery_id TEXT PRIMARY KEY,
                    inbox_id TEXT NOT NULL,
                    venue TEXT NOT NULL DEFAULT '',
                    market_id TEXT NOT NULL DEFAULT '',
                    attention_class TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_attempt_at TEXT,
                    sent_at TEXT,
                    http_status INTEGER,
                    error_code TEXT,
                    error_summary TEXT,
                    UNIQUE(channel, inbox_id)
                )
                """
            )
            existing_columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(alert_deliveries)").fetchall()
            }
            for column in ("venue", "market_id"):
                if column not in existing_columns:
                    conn.execute(
                        f"ALTER TABLE alert_deliveries ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS alert_deliveries_status_created
                ON alert_deliveries(status, created_at)
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS alert_deliveries_market_priority
                ON alert_deliveries(channel, venue, market_id, attention_class)
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS buy_alert_state (
                    sport TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    market_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    status TEXT NOT NULL,
                    matchup TEXT NOT NULL,
                    selected_side TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    game_start TEXT,
                    resolution_time TEXT,
                    last_price REAL,
                    model_probability REAL,
                    edge_points REAL,
                    closed_at TEXT,
                    close_reason TEXT,
                    PRIMARY KEY (sport, venue, market_id, side)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS buy_alert_state_status
                ON buy_alert_state(sport, status, updated_at)
                """
            )

    def mark_buy_active(self, item: dict[str, Any]) -> None:
        """Persist the lifecycle of a BUY only after it has been delivered."""
        now = utcnow().isoformat()
        key = (
            str(item["sport"]).upper(),
            str(item["venue"]),
            str(item["market_id"]),
            str(item["side"]),
        )
        with self._connect() as conn:
            existing = conn.execute(
                """
                SELECT status, activated_at
                FROM buy_alert_state
                WHERE sport = ? AND venue = ? AND market_id = ? AND side = ?
                """,
                key,
            ).fetchone()
            activated_at = (
                existing["activated_at"]
                if existing is not None and existing["status"] == "ACTIVE"
                else str(item.get("detected_at") or now)
            )
            conn.execute(
                """
                INSERT INTO buy_alert_state (
                    sport, venue, market_id, side, status, matchup, selected_side,
                    activated_at, updated_at, game_start, resolution_time,
                    last_price, model_probability, edge_points, closed_at, close_reason
                )
                VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                ON CONFLICT(sport, venue, market_id, side) DO UPDATE SET
                    status = 'ACTIVE',
                    matchup = excluded.matchup,
                    selected_side = excluded.selected_side,
                    activated_at = excluded.activated_at,
                    updated_at = excluded.updated_at,
                    game_start = excluded.game_start,
                    resolution_time = excluded.resolution_time,
                    last_price = excluded.last_price,
                    model_probability = excluded.model_probability,
                    edge_points = excluded.edge_points,
                    closed_at = NULL,
                    close_reason = NULL
                """,
                (
                    *key,
                    str(item.get("matchup") or ""),
                    str(item.get("selected_side") or item.get("side") or ""),
                    activated_at,
                    now,
                    item.get("game_start"),
                    item.get("resolution_time"),
                    item.get("executable_price"),
                    item.get("model_probability"),
                    item.get("edge_points"),
                ),
            )

    def buy_state(
        self,
        sport: str,
        venue: str,
        market_id: str,
        side: str,
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM buy_alert_state
                WHERE sport = ? AND venue = ? AND market_id = ? AND side = ?
                """,
                (sport.upper(), venue, market_id, side),
            ).fetchone()
        return None if row is None else dict(row)

    def active_buys(self, sport: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT *
                FROM buy_alert_state
                WHERE sport = ? AND status = 'ACTIVE'
                ORDER BY activated_at, venue, market_id, side
                """,
                (sport.upper(),),
            ).fetchall()
        return [dict(row) for row in rows]

    def close_buy(
        self,
        *,
        sport: str,
        venue: str,
        market_id: str,
        side: str,
        status: str,
        reason: str,
    ) -> bool:
        if status not in {"WITHDRAWN", "EXPIRED"}:
            raise ValueError("buy lifecycle close status must be WITHDRAWN or EXPIRED")
        now = utcnow().isoformat()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT status
                FROM buy_alert_state
                WHERE sport = ? AND venue = ? AND market_id = ? AND side = ?
                """,
                (sport.upper(), venue, market_id, side),
            ).fetchone()
            if row is None or row["status"] != "ACTIVE":
                return False
            conn.execute(
                """
                UPDATE buy_alert_state
                SET status = ?, updated_at = ?, closed_at = ?, close_reason = ?
                WHERE sport = ? AND venue = ? AND market_id = ? AND side = ?
                """,
                (
                    status,
                    now,
                    now,
                    reason,
                    sport.upper(),
                    venue,
                    market_id,
                    side,
                ),
            )
        return True

    @staticmethod
    def delivery_id(channel: str, inbox_id: str) -> str:
        return f"alert-{channel}-{inbox_id}"

    def ensure_delivery(self, item: dict[str, Any], channel: str) -> None:
        attention_class = str(item["attention_class"])
        priority = ALERT_PRIORITIES[attention_class]
        now = utcnow().isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO alert_deliveries (
                    delivery_id, inbox_id, venue, market_id, attention_class, priority, channel,
                    status, attempt_count, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PENDING', 0, ?)
                """,
                (
                    self.delivery_id(channel, str(item["inbox_id"])),
                    item["inbox_id"],
                    item.get("venue", ""),
                    item.get("market_id", ""),
                    attention_class,
                    priority,
                    channel,
                    now,
                ),
            )

    def pending(self, channel: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM alert_deliveries
                WHERE channel = ? AND status = 'PENDING' AND attempt_count < ?
                ORDER BY created_at
                """,
                (channel, MAX_ATTEMPTS),
            ).fetchall()
        return [dict(row) for row in rows]

    def has_actionable_delivery(
        self,
        channel: str,
        venue: str,
        market_id: str,
    ) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM alert_deliveries
                WHERE channel = ?
                    AND venue = ?
                    AND market_id = ?
                    AND attention_class = ?
                LIMIT 1
                """,
                (channel, venue, market_id, AttentionClass.ACTIONABLE_PLAY.value),
            ).fetchone()
        return row is not None

    def record_attempt(
        self,
        delivery_id: str,
        result: DeliveryResult,
        *,
        final: bool = True,
    ) -> None:
        attempted_at = utcnow().isoformat()
        sent_at = attempted_at if result.status == "SENT" else None
        with self._connect() as conn:
            row = conn.execute(
                "SELECT attempt_count FROM alert_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if row is None:
                return
            attempts = int(row["attempt_count"]) + 1
            status = result.status
            if not final and attempts < MAX_ATTEMPTS:
                status = "PENDING"
            conn.execute(
                """
                UPDATE alert_deliveries
                SET status = ?,
                    attempt_count = ?,
                    last_attempt_at = ?,
                    sent_at = COALESCE(sent_at, ?),
                    http_status = ?,
                    error_code = ?,
                    error_summary = ?
                WHERE delivery_id = ?
                """,
                (
                    status,
                    attempts,
                    attempted_at,
                    sent_at,
                    result.http_status,
                    result.error_code,
                    result.error_summary,
                    delivery_id,
                ),
            )

    def status(self, mode: str) -> dict[str, Any]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS count FROM alert_deliveries GROUP BY status"
            ).fetchall()
            last = conn.execute(
                """
                SELECT MAX(COALESCE(sent_at, last_attempt_at, created_at)) AS value
                FROM alert_deliveries
                """
            ).fetchone()
        counts = {row["status"].casefold(): row["count"] for row in rows}
        return {
            "mode": mode,
            "pending": counts.get("pending", 0),
            "sent": counts.get("sent", 0),
            "failed": counts.get("failed", 0),
            "unknown": counts.get("unknown", 0),
            "last_delivery_at": None if last is None else last["value"],
        }

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(100, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT delivery_id, inbox_id, attention_class, priority, status,
                    attempt_count, created_at, last_attempt_at, sent_at,
                    http_status, error_code, error_summary
                FROM alert_deliveries
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def delivery(self, channel: str, inbox_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM alert_deliveries WHERE channel = ? AND inbox_id = ?",
                (channel, inbox_id),
            ).fetchone()
        return None if row is None else dict(row)


class AlertDispatcher:
    def __init__(
        self,
        store: AlertDeliveryStore,
        config: AlertConfig | None = None,
        transport: AlertTransport | None = None,
    ):
        self.store = store
        self.transport = transport or WebhookTransport()
        if _pytest_active() and (
            config is None or isinstance(self.transport, WebhookTransport)
        ):
            # Test-created services must never inherit operator alert config.
            self.config = AlertConfig(mode="disabled")
        else:
            self.config = config or AlertConfig.load()

    @property
    def channel(self) -> str:
        return self.config.mode

    def dispatch(self, inbox_items: list[dict[str, Any]]) -> None:
        if _pytest_active() and isinstance(self.transport, WebhookTransport):
            return
        if self.config.mode == "disabled":
            return
        eligible = [
            item
            for item in inbox_items
            if item.get("attention_class") in ALERT_PRIORITIES
            and item.get("status") == "ACTIVE"
            and (
                item.get("attention_class") == AttentionClass.ACTIONABLE_PLAY.value
                or not self.store.has_actionable_delivery(
                    self.channel,
                    str(item.get("venue", "")),
                    str(item.get("market_id", "")),
                )
            )
        ]
        for item in eligible:
            self.store.ensure_delivery(item, self.channel)
        for delivery in self.store.pending(self.channel):
            pending_item: dict[str, Any] | None = None
            for row in eligible:
                if row["inbox_id"] == delivery["inbox_id"]:
                    pending_item = row
                    break
            if pending_item is None:
                continue
            if self.config.mode == "dry_run":
                self.store.record_attempt(
                    delivery["delivery_id"],
                    DeliveryResult("SENT"),
                )
                continue
            if self.config.mode == "ntfy":
                if not self.config.ntfy_topic:
                    result = DeliveryResult(
                        "FAILED",
                        error_code="CONFIGURATION_ERROR",
                        error_summary="ntfy mode requires PARALLAX_ALERT_NTFY_TOPIC.",
                    )
                    self.store.record_attempt(delivery["delivery_id"], result)
                    logger.error(
                        "PARALLAX ntfy delivery status=FAILED http_status=None error_code=%s delivery_id=%s",
                        result.error_code,
                        delivery["delivery_id"],
                    )
                    continue
                try:
                    result = self.transport.post_json(
                        ntfy_publish_url(self.config.ntfy_server),
                        ntfy_payload(
                            pending_item,
                            delivery["priority"],
                            self.config.ntfy_topic,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001 - delivery cannot stop scoring
                    result = DeliveryResult(
                        "FAILED",
                        error_code="TRANSPORT_EXCEPTION",
                        error_summary=f"Alert transport raised {type(exc).__name__}.",
                    )
                retryable = result.error_code in {"TIMEOUT", "HTTP_5XX"}
                final = not retryable or delivery["attempt_count"] + 1 >= MAX_ATTEMPTS
                if result.status == "UNKNOWN":
                    final = True
                self.store.record_attempt(delivery["delivery_id"], result, final=final)
                if result.status != "SENT":
                    logger.error(
                        "PARALLAX ntfy delivery status=%s http_status=%s error_code=%s delivery_id=%s",
                        result.status,
                        result.http_status,
                        result.error_code,
                        delivery["delivery_id"],
                    )
                continue
            if not self.config.webhook_url:
                self.store.record_attempt(
                    delivery["delivery_id"],
                    DeliveryResult(
                        "FAILED",
                        error_code="CONFIGURATION_ERROR",
                        error_summary="Webhook mode requires PARALLAX_ALERT_WEBHOOK_URL.",
                    ),
                )
                continue
            result = self.transport.post_json(
                self.config.webhook_url,
                alert_payload(pending_item, delivery["priority"]),
            )
            retryable = result.error_code in {"TIMEOUT", "HTTP_5XX"}
            final = not retryable or delivery["attempt_count"] + 1 >= MAX_ATTEMPTS
            if result.status == "UNKNOWN":
                final = True
            self.store.record_attempt(delivery["delivery_id"], result, final=final)

    def status(self) -> dict[str, Any]:
        return self.store.status(self.config.mode)

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.recent(limit)


def alert_payload(item: dict[str, Any], priority: str | None = None) -> dict[str, Any]:
    priority = priority or ALERT_PRIORITIES[str(item["attention_class"])]
    sent_at = utcnow().isoformat()
    payload = {
        "source": "PARALLAX",
        "priority": priority,
        "attention_class": item["attention_class"],
        "inbox_id": item["inbox_id"],
        "headline": item["headline"],
        "market": item["market"],
        "verdict": item["verdict"],
        "actionability": item["actionability"],
        "trade_confidence": item["trade_confidence"],
        "message": format_alert_message(item),
        "detected_at": item["detected_at"],
        "sent_at": sent_at,
    }
    return payload


def ntfy_payload(item: dict[str, Any], priority: str, topic: str) -> dict[str, Any]:
    return {
        "topic": topic,
        "message": format_alert_message(item),
        "title": (
            "PARALLAX BUY"
            if item.get("alert_kind") == "SCORED_BUY"
            else "PARALLAX BUY WITHDRAWN"
            if item.get("alert_kind") == "SCORED_BUY_WITHDRAWN"
            else "PARALLAX BUY EXPIRED"
            if item.get("alert_kind") == "SCORED_BUY_EXPIRED"
            else ALERT_TITLES[str(item["attention_class"])]
        ),
        "priority": NTFY_PRIORITIES[priority],
    }


def ntfy_publish_url(server: str) -> str:
    return f"{server.rstrip('/')}/"


def format_alert_message(item: dict[str, Any]) -> str:
    if item.get("alert_kind") == "SCORED_BUY":
        return _scored_buy_message(item)
    if item.get("alert_kind") in {"SCORED_BUY_WITHDRAWN", "SCORED_BUY_EXPIRED"}:
        return _scored_buy_lifecycle_message(item)
    attention_class = item["attention_class"]
    if attention_class == AttentionClass.ACTIONABLE_PLAY.value:
        return _actionable_message(item)
    if attention_class == AttentionClass.PUBLIC_WORTHY.value:
        return "\n".join(
            (
                "PARALLAX PUBLIC-WORTHY SIGNAL",
                "",
                f"Market: {item['market']}",
                f"Signal: {item['source_signal']['signal_type']}",
                f"What happened: {item['what_happened']}",
                f"What it means: {item['what_it_means']}",
                "",
                "Verdict: WATCH",
                "Not a BUY recommendation.",
            )
        )
    return "\n".join(
        (
            "PARALLAX PRIORITY WATCH",
            "",
            f"Market: {item['market']}",
            f"What happened: {item['what_happened']}",
            f"What it means: {item['what_it_means']}",
            f"Trade confidence: {item['trade_confidence']}",
            "",
            "Operator:",
            "Watch for confirmation. No trade unless promoted to a PARALLAX Play.",
        )
    )


def dispatch_scored_buy(
    dispatcher: AlertDispatcher,
    play: ParallaxPlay,
    market: NormalizedMarket,
    *,
    sport: str,
    matchup: str,
    detected_at: str,
    game_start: str | None = None,
) -> dict[str, Any] | None:
    """Dispatch one deduplicated immediate alert for a freshly scored BUY."""
    if play.suggested_action != Action.BUY:
        return None
    prior_state = dispatcher.store.buy_state(
        sport,
        str(play.venue),
        play.market_id,
        str(play.side),
    )
    lifecycle_generation = (
        str(prior_state.get("closed_at") or prior_state.get("updated_at") or "")
        if prior_state is not None and prior_state.get("status") != "ACTIVE"
        else None
    )
    item = _scored_buy_item(
        play,
        market,
        sport=sport,
        matchup=matchup,
        detected_at=detected_at,
        game_start=game_start,
        lifecycle_generation=lifecycle_generation,
    )
    existing = dispatcher.store.delivery(dispatcher.channel, item["inbox_id"])
    dispatcher.dispatch([item])
    delivery = dispatcher.store.delivery(dispatcher.channel, item["inbox_id"])
    if delivery is None:
        return {
            "status": "DISABLED",
            "deduplicated": False,
            "http_status": None,
            "error_code": None,
        }
    if delivery["status"] == "SENT":
        dispatcher.store.mark_buy_active(item)
    return {
        "status": delivery["status"],
        "deduplicated": existing is not None,
        "http_status": delivery["http_status"],
        "error_code": delivery["error_code"],
    }


def reconcile_active_buy_alerts(
    dispatcher: AlertDispatcher,
    plays: list[ParallaxPlay],
    *,
    sport: str,
    detected_at: str,
) -> dict[str, int]:
    """Close only previously delivered BUYs with explicit invalidation evidence."""
    now = timestamp(detected_at) or utcnow()
    current = {
        (
            str(play.venue),
            play.market_id,
            str(play.side),
        ): play
        for play in plays
    }
    summary = {"withdrawn": 0, "expired": 0, "failed": 0}
    for active in dispatcher.store.active_buys(sport):
        key = (active["venue"], active["market_id"], active["side"])
        play = current.get(key)
        lifecycle = None
        reason = None
        replacement_action = None
        game_start = timestamp(active.get("game_start"))
        active_resolution = timestamp(active.get("resolution_time"))
        play_resolution = timestamp(play.resolution_time) if play is not None else None
        if game_start is not None and game_start <= now:
            lifecycle = "EXPIRED"
            reason = "The scheduled game has started."
        elif (
            (play_resolution is not None and play_resolution <= now)
            or (active_resolution is not None and active_resolution <= now)
        ):
            lifecycle = "EXPIRED"
            reason = "The market resolution window has passed."
        elif play is not None and play.suggested_action != Action.BUY:
            lifecycle = "WITHDRAWN"
            replacement_action = str(play.suggested_action)
            reason = f"PARALLAX rescored this position as {replacement_action}."

        if lifecycle is None or reason is None:
            continue
        item = _scored_buy_lifecycle_item(
            active,
            lifecycle=lifecycle,
            reason=reason,
            replacement_action=replacement_action,
            detected_at=detected_at,
        )
        existing = dispatcher.store.delivery(dispatcher.channel, item["inbox_id"])
        try:
            dispatcher.dispatch([item])
            delivery = dispatcher.store.delivery(dispatcher.channel, item["inbox_id"])
        except Exception:
            summary["failed"] += 1
            continue
        if delivery is None or delivery["status"] != "SENT":
            if delivery is not None and delivery["status"] in {"FAILED", "UNKNOWN", "PENDING"}:
                summary["failed"] += 1
            continue
        if not dispatcher.store.close_buy(
            sport=sport,
            venue=active["venue"],
            market_id=active["market_id"],
            side=active["side"],
            status=lifecycle,
            reason=reason,
        ):
            continue
        if existing is None:
            summary["expired" if lifecycle == "EXPIRED" else "withdrawn"] += 1
    return summary


def _scored_buy_item(
    play: ParallaxPlay,
    market: NormalizedMarket,
    *,
    sport: str,
    matchup: str,
    detected_at: str,
    game_start: str | None = None,
    lifecycle_generation: str | None = None,
) -> dict[str, Any]:
    material_state = {
        "venue": str(play.venue),
        "market_id": play.market_id,
        "side": str(play.side),
        "price": _rounded(play.executable_price, 2),
        "probability": _rounded(play.model_probability, 2),
        "edge_points": _rounded(play.edge_points, 1),
        "liquidity": _rounded(play.executable_size, 0),
        "lifecycle_generation": lifecycle_generation,
    }
    fingerprint = hashlib.sha256(
        json.dumps(material_state, allow_nan=False, sort_keys=True).encode()
    ).hexdigest()[:16]
    selected_side = play.side_description or str(play.side)
    return {
        "inbox_id": f"inbox-scored-buy-{play.venue}-{play.market_id}-{fingerprint}",
        "attention_class": AttentionClass.ACTIONABLE_PLAY.value,
        "status": "ACTIVE",
        "venue": str(play.venue),
        "market_id": play.market_id,
        "headline": "PARALLAX BUY",
        "market": play.market_title,
        "verdict": f"BUY {play.side}",
        "actionability": "ACTIONABLE",
        "trade_confidence": str(play.confidence_band),
        "detected_at": detected_at,
        "alert_kind": "SCORED_BUY",
        "sport": sport.upper(),
        "matchup": matchup,
        "selected_side": selected_side,
        "side": str(play.side),
        "game_start": (
            game_start
            or (
                str(market.original_metadata.get("market", {}).get("gameStartTime"))
                if isinstance(market.original_metadata.get("market"), dict)
                and market.original_metadata.get("market", {}).get("gameStartTime")
                else None
            )
        ),
        "resolution_time": play.resolution_time,
        "executable_price": play.executable_price,
        "model_probability": play.model_probability,
        "edge_points": play.edge_points,
        "liquidity": play.executable_size,
        "contract_url": market.source_url or play.market_url,
    }


def _scored_buy_lifecycle_item(
    active: dict[str, Any],
    *,
    lifecycle: str,
    reason: str,
    replacement_action: str | None,
    detected_at: str,
) -> dict[str, Any]:
    identity = "|".join(
        (
            active["sport"],
            active["venue"],
            active["market_id"],
            active["side"],
            active["activated_at"],
            lifecycle,
        )
    )
    fingerprint = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return {
        "inbox_id": f"inbox-scored-buy-{lifecycle.casefold()}-{fingerprint}",
        "attention_class": AttentionClass.ACTIONABLE_PLAY.value,
        "status": "ACTIVE",
        "venue": active["venue"],
        "market_id": active["market_id"],
        "headline": f"PARALLAX BUY {lifecycle}",
        "market": active["matchup"],
        "verdict": lifecycle,
        "actionability": "ACTION_REQUIRED",
        "trade_confidence": "N/A",
        "detected_at": detected_at,
        "alert_kind": (
            "SCORED_BUY_EXPIRED"
            if lifecycle == "EXPIRED"
            else "SCORED_BUY_WITHDRAWN"
        ),
        "sport": active["sport"],
        "matchup": active["matchup"],
        "selected_side": active["selected_side"],
        "reason": reason,
        "replacement_action": replacement_action,
    }


def _scored_buy_lifecycle_message(item: dict[str, Any]) -> str:
    label = "EXPIRED" if item["alert_kind"] == "SCORED_BUY_EXPIRED" else "WITHDRAWN"
    lines = [
        f"PARALLAX BUY {label}",
        f"Sport: {item['sport']}",
        f"Venue: {item['venue']}",
        f"Matchup: {item['matchup']}",
        f"Selected side: {item['selected_side']}",
        f"Reason: {item['reason']}",
    ]
    if item.get("replacement_action"):
        lines.append(f"Current verdict: {item['replacement_action']}")
    lines.append(f"Timestamp: {item['detected_at']}")
    return "\n".join(lines)


def _rounded(value: float | None, digits: int) -> float | None:
    return None if value is None else round(float(value), digits)


def _scored_buy_message(item: dict[str, Any]) -> str:
    lines = [
        "PARALLAX BUY",
        f"Sport: {item['sport']}",
        f"Venue: {item['venue']}",
        f"Matchup: {item['matchup']}",
        f"Selected side: {item['selected_side']}",
        f"Executable price: {_format_price(item.get('executable_price'))}",
        f"Model probability: {_format_probability(item.get('model_probability'))}",
        f"Edge: {_format_edge(item.get('edge_points'))}",
        f"Liquidity: {_format_liquidity(item.get('liquidity'))}",
    ]
    if item.get("contract_url"):
        lines.append(f"Contract: {item['contract_url']}")
    lines.append(f"Timestamp: {item['detected_at']}")
    return "\n".join(lines)


def _format_probability(value: Any) -> str:
    return "unavailable" if not isinstance(value, int | float) else f"{value * 100:.1f}%"


def _format_price(value: Any) -> str:
    return "unavailable" if not isinstance(value, int | float) else f"{value * 100:.1f}¢"


def _format_edge(value: Any) -> str:
    return "unavailable" if not isinstance(value, int | float) else f"{value:.1f} pp"


def _format_liquidity(value: Any) -> str:
    return "unavailable" if not isinstance(value, int | float) else f"{value:,.0f} contracts"


def _actionable_message(item: dict[str, Any]) -> str:
    economics = item.get("economics", {})
    example_lines = []
    for example in economics.get("examples", ()):
        if not example.get("available", True):
            continue
        stake = example.get("stake")
        profit = example.get("gross_profit_if_correct")
        if stake is None or profit is None:
            continue
        example_lines.append(
            f"${stake:,.0f} risk -> potential gross +${profit:,.0f}"
        )
    current_market = item.get("current_market", {})
    side = str(item["verdict"]).removeprefix("BUY ").lower()
    price = current_market.get("current_executable_buy_prices", {}).get(side.upper())
    price_line = []
    if isinstance(price, int | float):
        price_line = [f"Current price: {price * 100:.0f}¢", ""]
    why = item.get("why") or item["what_it_means"]
    return "\n".join(
        (
            "PARALLAX ACTIONABLE PLAY",
            "",
            f"Market: {item['market']}",
            f"Verdict: {item['verdict']}",
            f"Trade confidence: {item['trade_confidence']}",
            *price_line,
            *example_lines,
            "",
            "Why:",
            why,
            "",
            "Operator:",
            "Review PARALLAX Play now.",
            "",
            "Maximum loss equals amount spent.",
            "Before fees/costs.",
        )
    )


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"'")
    return values
