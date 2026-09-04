from __future__ import annotations

import fcntl
import json
import math
import os
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol, Self

LIVE_ENABLED_VALUE = "YES"
KILL_SWITCH_VALUE = "YES"
LIVE_ENV = "PARALLAX_LIVE_ENABLED"
KILL_ENV = "PARALLAX_KILL_SWITCH"
KILL_FILE_ENV = "PARALLAX_KILL_SWITCH_FILE"


class SafetyStop(RuntimeError):
    """A condition that requires all live orders to be cancelled and quoting to stop."""


class ReconciliationError(SafetyStop):
    """Local and venue execution state cannot be reconciled safely."""


def require_geographic_eligibility(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("blocked") is not False:
        country = payload.get("country") if isinstance(payload, dict) else "UNKNOWN"
        region = payload.get("region") if isinstance(payload, dict) else "UNKNOWN"
        raise SafetyStop(
            f"Polymarket geographic eligibility blocked or unknown: {country}/{region}"
        )


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _float(value: Any, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ReconciliationError(f"invalid {name}: {value!r}") from exc
    if not math.isfinite(result):
        raise ReconciliationError(f"non-finite {name}")
    return result


def _json(value: Any) -> str:
    return json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class LiveLimits:
    total_bankroll_usd: float = 30.0
    max_deployed_usd: float = 20.0
    per_market_usd: float = 8.0
    per_order_usd: float = 4.0
    max_inventory_usd_per_market: float = 8.0
    max_active_markets: int = 3
    max_daily_loss_usd: float = 2.0
    max_drawdown_usd: float = 3.0
    stale_websocket_seconds: float = 15.0
    stale_book_seconds: float = 10.0
    max_quote_age_seconds: float = 20.0
    material_book_move_ticks: int = 1
    reconciliation_seconds: float = 3.0
    heartbeat_seconds: float = 5.0
    discovery_seconds: float = 60.0
    adverse_selection_bps: float = 25.0
    capital_lock_bps_per_hour: float = 1.0
    min_expected_net_usd_per_hour: float = 0.001
    book_scan_limit: int = 100

    def __post_init__(self) -> None:
        positive = {
            "total_bankroll_usd": self.total_bankroll_usd,
            "max_deployed_usd": self.max_deployed_usd,
            "per_market_usd": self.per_market_usd,
            "per_order_usd": self.per_order_usd,
            "max_inventory_usd_per_market": self.max_inventory_usd_per_market,
            "max_daily_loss_usd": self.max_daily_loss_usd,
            "max_drawdown_usd": self.max_drawdown_usd,
            "stale_websocket_seconds": self.stale_websocket_seconds,
            "stale_book_seconds": self.stale_book_seconds,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "reconciliation_seconds": self.reconciliation_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "discovery_seconds": self.discovery_seconds,
        }
        if any(not math.isfinite(value) or value <= 0 for value in positive.values()):
            raise ValueError("all live dollar/time limits must be finite and positive")
        if not 1 <= self.max_active_markets <= 3:
            raise ValueError("max_active_markets must be between 1 and 3")
        if self.max_deployed_usd > self.total_bankroll_usd:
            raise ValueError("max_deployed_usd cannot exceed total bankroll")
        if self.per_order_usd > self.per_market_usd:
            raise ValueError("per_order_usd cannot exceed per-market capital")
        if self.per_market_usd > self.max_deployed_usd:
            raise ValueError(
                "per-market capital cannot exceed maximum deployed capital"
            )
        if self.max_inventory_usd_per_market > self.per_market_usd:
            raise ValueError("inventory cap cannot exceed per-market capital")
        if (
            self.material_book_move_ticks < 1
            or self.book_scan_limit < self.max_active_markets
        ):
            raise ValueError("invalid market scan or material-move limit")
        if self.adverse_selection_bps < 0 or self.capital_lock_bps_per_hour < 0:
            raise ValueError("economic risk deductions cannot be negative")

    @classmethod
    def from_env(cls) -> LiveLimits:
        mapping: dict[str, tuple[str, type]] = {
            "total_bankroll_usd": ("PARALLAX_TOTAL_BANKROLL_USD", float),
            "max_deployed_usd": ("PARALLAX_MAX_DEPLOYED_USD", float),
            "per_market_usd": ("PARALLAX_PER_MARKET_USD", float),
            "per_order_usd": ("PARALLAX_PER_ORDER_USD", float),
            "max_inventory_usd_per_market": ("PARALLAX_MAX_INVENTORY_USD", float),
            "max_active_markets": ("PARALLAX_MAX_ACTIVE_MARKETS", int),
            "max_daily_loss_usd": ("PARALLAX_MAX_DAILY_LOSS_USD", float),
            "max_drawdown_usd": ("PARALLAX_MAX_DRAWDOWN_USD", float),
            "stale_websocket_seconds": ("PARALLAX_STALE_WEBSOCKET_SECONDS", float),
            "stale_book_seconds": ("PARALLAX_STALE_BOOK_SECONDS", float),
            "max_quote_age_seconds": ("PARALLAX_MAX_QUOTE_AGE_SECONDS", float),
            "material_book_move_ticks": ("PARALLAX_MATERIAL_BOOK_MOVE_TICKS", int),
            "reconciliation_seconds": ("PARALLAX_RECONCILIATION_SECONDS", float),
            "heartbeat_seconds": ("PARALLAX_HEARTBEAT_SECONDS", float),
            "discovery_seconds": ("PARALLAX_DISCOVERY_SECONDS", float),
            "adverse_selection_bps": ("PARALLAX_ADVERSE_SELECTION_BPS", float),
            "capital_lock_bps_per_hour": ("PARALLAX_CAPITAL_LOCK_BPS_PER_HOUR", float),
            "min_expected_net_usd_per_hour": (
                "PARALLAX_MIN_EXPECTED_NET_USD_PER_HOUR",
                float,
            ),
            "book_scan_limit": ("PARALLAX_BOOK_SCAN_LIMIT", int),
        }
        values: dict[str, Any] = {}
        for field_name, (env_name, cast) in mapping.items():
            raw = os.environ.get(env_name)
            if raw not in (None, ""):
                try:
                    values[field_name] = cast(raw)
                except ValueError as exc:
                    raise ValueError(f"{env_name} has an invalid value") from exc
        return cls(**values)


@dataclass(frozen=True)
class Candidate:
    market_id: str
    event_id: str
    token_id: str
    outcome: str
    question: str
    best_bid: float
    best_ask: float
    bid_size_shares: float
    ask_size_shares: float
    tick_size: float
    min_order_size_shares: float
    volume_24h_usd: float
    liquidity_usd: float
    accepting_orders: bool = True
    market_active: bool = True
    book_observed_monotonic: float = 0.0

    @property
    def midpoint(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


@dataclass(frozen=True)
class RankedCandidate:
    candidate: Candidate
    quote_size_shares: float
    expected_net_usd_per_hour: float
    queue_fill_proxy: float
    turnover_per_hour_proxy: float


@dataclass(frozen=True)
class OrderIntent:
    market_id: str
    event_id: str
    token_id: str
    outcome: str
    side: str
    price: float
    size_shares: float
    tick_size: float
    expected_net_usd_per_hour: float

    @property
    def notional_usd(self) -> float:
        return self.price * self.size_shares


class Venue(Protocol):
    def place_post_only(self, intent: OrderIntent) -> dict[str, Any]: ...
    def cancel_order(self, order_id: str) -> dict[str, Any]: ...
    def cancel_all(self) -> dict[str, Any]: ...
    def get_open_orders(self) -> list[dict[str, Any]]: ...
    def get_order(self, order_id: str) -> dict[str, Any]: ...
    def get_trades(self, *, after: int | None = None) -> list[dict[str, Any]]: ...
    def heartbeat(self, heartbeat_id: str) -> dict[str, Any]: ...
    def confirmed_rewards(self, date: str) -> list[dict[str, Any]]: ...


def rank_candidate(candidate: Candidate, limits: LiveLimits) -> RankedCandidate | None:
    if (
        not candidate.accepting_orders
        or not candidate.market_active
        or not 0 < candidate.best_bid < candidate.best_ask < 1
        or candidate.tick_size <= 0
    ):
        return None
    shares = min(
        limits.per_order_usd / candidate.best_bid,
        max(candidate.min_order_size_shares, candidate.bid_size_shares),
    )
    if shares + 1e-9 < candidate.min_order_size_shares:
        return None
    shares = math.floor(shares * 100) / 100
    if shares <= 0 or shares * candidate.best_bid > limits.per_order_usd + 1e-9:
        return None
    queue_fill = min(1.0, shares / max(shares + candidate.bid_size_shares, 1e-9))
    turnover = max(
        0.01,
        min(4.0, candidate.volume_24h_usd / max(candidate.liquidity_usd, 1.0) / 24.0),
    )
    gross_capture = (candidate.best_ask - candidate.best_bid) * shares
    notional = shares * candidate.best_bid
    adverse = notional * limits.adverse_selection_bps / 10_000.0
    capital_lock = notional * limits.capital_lock_bps_per_hour / 10_000.0
    expected = gross_capture * queue_fill * turnover - adverse - capital_lock
    if expected < limits.min_expected_net_usd_per_hour:
        return None
    return RankedCandidate(candidate, shares, expected, queue_fill, turnover)


class LiveStore:
    SCHEMA = """
    PRAGMA foreign_keys=ON;
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS live_runs (
      run_id TEXT PRIMARY KEY, mode TEXT NOT NULL CHECK(mode IN ('DRY_RUN','LIVE')),
      started_at_utc TEXT NOT NULL, stopped_at_utc TEXT, stop_reason TEXT,
      capital_allocated_usd REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS live_orders (
      local_order_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES live_runs(run_id),
      venue_order_id TEXT UNIQUE, market_id TEXT NOT NULL, event_id TEXT NOT NULL,
      token_id TEXT NOT NULL, outcome TEXT NOT NULL, side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
      submitted_price REAL NOT NULL, original_quantity REAL NOT NULL, filled_quantity REAL NOT NULL DEFAULT 0,
      submitted_at_utc TEXT NOT NULL, acknowledged_at_utc TEXT, last_update_at_utc TEXT NOT NULL,
      status TEXT NOT NULL, post_only INTEGER NOT NULL CHECK(post_only=1), order_latency_ms REAL,
      expected_net_usd_per_hour REAL NOT NULL, cancel_reason TEXT, raw_ack_json TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_live_orders_status ON live_orders(status);
    CREATE TABLE IF NOT EXISTS live_fills (
      external_fill_key TEXT PRIMARY KEY, timestamp_utc TEXT NOT NULL,
      market_id TEXT NOT NULL, event_id TEXT NOT NULL, token_id TEXT NOT NULL, outcome TEXT NOT NULL,
      order_id TEXT NOT NULL, maker_side TEXT NOT NULL CHECK(maker_side IN ('BUY','SELL')),
      submitted_price REAL NOT NULL, fill_price REAL NOT NULL, filled_quantity REAL NOT NULL,
      notional_usd REAL NOT NULL, fees_usd REAL NOT NULL, rebate_amount_usd REAL,
      liquidity_reward_usd REAL, inventory_before_shares REAL NOT NULL,
      inventory_after_shares REAL NOT NULL, realized_trading_pnl_usd REAL NOT NULL,
      realized_inventory_pnl_usd REAL NOT NULL, cumulative_realized_net_pnl_usd REAL NOT NULL,
      raw_fill_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS live_inventory (
      token_id TEXT PRIMARY KEY, market_id TEXT NOT NULL, event_id TEXT NOT NULL, outcome TEXT NOT NULL,
      quantity_shares REAL NOT NULL, average_cost_usd REAL NOT NULL,
      realized_trading_pnl_usd REAL NOT NULL, realized_inventory_pnl_usd REAL NOT NULL,
      fees_usd REAL NOT NULL, updated_at_utc TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS live_confirmed_income (
      income_key TEXT PRIMARY KEY, date_utc TEXT NOT NULL, market_id TEXT NOT NULL,
      kind TEXT NOT NULL CHECK(kind IN ('MAKER_REBATE','LIQUIDITY_REWARD')),
      amount_usd REAL NOT NULL, source TEXT NOT NULL, raw_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS live_equity (
      id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_utc TEXT NOT NULL,
      realized_net_pnl_usd REAL NOT NULL, unrealized_inventory_pnl_usd REAL NOT NULL,
      equity_pnl_usd REAL NOT NULL, peak_equity_pnl_usd REAL NOT NULL, drawdown_usd REAL NOT NULL
    );
    CREATE TABLE IF NOT EXISTS live_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp_utc TEXT NOT NULL,
      kind TEXT NOT NULL, detail_json TEXT NOT NULL
    );
    """

    def __init__(
        self, path: Path | str, *, mode: str, capital_allocated_usd: float
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=5.0)
        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.executescript(self.SCHEMA)
            integrity = self.conn.execute("PRAGMA quick_check").fetchone()[0]
            if integrity != "ok":
                raise SafetyStop(f"database integrity check failed: {integrity}")
        except Exception:
            self.conn.close()
            raise
        self.run_id = uuid.uuid4().hex
        with self.conn:
            self.conn.execute(
                "INSERT INTO live_runs(run_id,mode,started_at_utc,capital_allocated_usd) VALUES(?,?,?,?)",
                (self.run_id, mode, utc_now(), capital_allocated_usd),
            )

    def close(self, reason: str) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE live_runs SET stopped_at_utc=?,stop_reason=? WHERE run_id=?",
                (utc_now(), reason, self.run_id),
            )
        self.conn.close()

    def event(self, kind: str, detail: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO live_events(timestamp_utc,kind,detail_json) VALUES(?,?,?)",
                (utc_now(), kind, _json(detail)),
            )

    def submit_order(self, intent: OrderIntent, *, mode: str) -> str:
        local_id = uuid.uuid4().hex
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """INSERT INTO live_orders
                (local_order_id,run_id,market_id,event_id,token_id,outcome,side,submitted_price,
                 original_quantity,submitted_at_utc,last_update_at_utc,status,post_only,
                 expected_net_usd_per_hour,raw_ack_json)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)""",
                (
                    local_id,
                    self.run_id,
                    intent.market_id,
                    intent.event_id,
                    intent.token_id,
                    intent.outcome,
                    intent.side,
                    intent.price,
                    intent.size_shares,
                    now,
                    now,
                    "DRY_RUN_RESTING" if mode == "DRY_RUN" else "SUBMITTED",
                    intent.expected_net_usd_per_hour,
                    "{}",
                ),
            )
        return local_id

    def acknowledge_order(
        self,
        local_id: str,
        *,
        venue_order_id: str,
        status: str,
        latency_ms: float,
        raw: dict[str, Any],
    ) -> None:
        now = utc_now()
        with self.conn:
            self.conn.execute(
                """UPDATE live_orders SET venue_order_id=?,status=?,acknowledged_at_utc=?,
                last_update_at_utc=?,order_latency_ms=?,raw_ack_json=? WHERE local_order_id=?""",
                (venue_order_id, status, now, now, latency_ms, _json(raw), local_id),
            )

    def reject_order(self, local_id: str, raw: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE live_orders SET status='REJECTED',last_update_at_utc=?,raw_ack_json=? WHERE local_order_id=?",
                (utc_now(), _json(raw), local_id),
            )

    def open_orders(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM live_orders WHERE status IN
            ('SUBMITTED','ACKNOWLEDGED','RESTING','PARTIALLY_FILLED','DRY_RUN_RESTING')"""
        ).fetchall()

    def order_by_venue_id(self, order_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM live_orders WHERE venue_order_id=?", (order_id,)
        ).fetchone()

    def set_order_state(
        self,
        order_id: str,
        status: str,
        filled_quantity: float,
        *,
        reason: str | None = None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """UPDATE live_orders SET status=?,filled_quantity=?,last_update_at_utc=?,
                cancel_reason=COALESCE(?,cancel_reason) WHERE venue_order_id=?""",
                (status, filled_quantity, utc_now(), reason, order_id),
            )

    def inventory(self, token_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM live_inventory WHERE token_id=?", (token_id,)
        ).fetchone()

    def all_inventory(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM live_inventory WHERE quantity_shares>1e-9"
        ).fetchall()

    def _income_total(self, kind: str | None = None) -> float:
        if kind:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(amount_usd),0) FROM live_confirmed_income WHERE kind=?",
                (kind,),
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT COALESCE(SUM(amount_usd),0) FROM live_confirmed_income"
            ).fetchone()
        return float(row[0])

    def realized_net(self) -> float:
        row = self.conn.execute(
            """SELECT COALESCE(SUM(realized_trading_pnl_usd+realized_inventory_pnl_usd-fees_usd
            +COALESCE(rebate_amount_usd,0)+COALESCE(liquidity_reward_usd,0)),0)
            FROM live_fills"""
        ).fetchone()
        return float(row[0]) + self._income_total()

    def record_fill(
        self,
        *,
        external_fill_key: str,
        order: sqlite3.Row,
        timestamp_utc: str,
        fill_price: float,
        quantity: float,
        fees_usd: float,
        raw: dict[str, Any],
        rebate_amount_usd: float = 0.0,
        liquidity_reward_usd: float = 0.0,
    ) -> bool:
        if (
            quantity <= 0
            or fill_price <= 0
            or fill_price >= 1
            or fees_usd < 0
            or rebate_amount_usd < 0
            or liquidity_reward_usd < 0
        ):
            raise ReconciliationError("invalid confirmed fill economics")
        if self.conn.execute(
            "SELECT 1 FROM live_fills WHERE external_fill_key=?", (external_fill_key,)
        ).fetchone():
            return False
        current = self.inventory(order["token_id"])
        before = float(current["quantity_shares"]) if current else 0.0
        average = float(current["average_cost_usd"]) if current else 0.0
        prior_trading = float(current["realized_trading_pnl_usd"]) if current else 0.0
        prior_inventory = (
            float(current["realized_inventory_pnl_usd"]) if current else 0.0
        )
        prior_fees = float(current["fees_usd"]) if current else 0.0
        if order["side"] == "BUY":
            after = before + quantity
            average = ((before * average) + (quantity * fill_price)) / after
            realized_trading = 0.0
        else:
            if quantity > before + 1e-9:
                raise ReconciliationError(
                    "confirmed sell fill exceeds locally owned inventory"
                )
            after = max(0.0, before - quantity)
            realized_trading = (fill_price - average) * quantity
            if after <= 1e-9:
                average = 0.0
        cumulative_before = self.realized_net()
        cumulative_after = (
            cumulative_before
            + realized_trading
            - fees_usd
            + rebate_amount_usd
            + liquidity_reward_usd
        )
        with self.conn:
            self.conn.execute(
                """INSERT INTO live_fills
                (external_fill_key,timestamp_utc,market_id,event_id,token_id,outcome,order_id,
                 maker_side,submitted_price,fill_price,filled_quantity,notional_usd,fees_usd,
                 rebate_amount_usd,liquidity_reward_usd,inventory_before_shares,inventory_after_shares,
                 realized_trading_pnl_usd,realized_inventory_pnl_usd,cumulative_realized_net_pnl_usd,
                 raw_fill_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    external_fill_key,
                    timestamp_utc,
                    order["market_id"],
                    order["event_id"],
                    order["token_id"],
                    order["outcome"],
                    order["venue_order_id"],
                    order["side"],
                    order["submitted_price"],
                    fill_price,
                    quantity,
                    fill_price * quantity,
                    fees_usd,
                    rebate_amount_usd,
                    liquidity_reward_usd,
                    before,
                    after,
                    realized_trading,
                    0.0,
                    cumulative_after,
                    _json(raw),
                ),
            )
            self.conn.execute(
                """INSERT INTO live_inventory
                (token_id,market_id,event_id,outcome,quantity_shares,average_cost_usd,
                 realized_trading_pnl_usd,realized_inventory_pnl_usd,fees_usd,updated_at_utc)
                 VALUES(?,?,?,?,?,?,?,?,?,?)
                 ON CONFLICT(token_id) DO UPDATE SET quantity_shares=excluded.quantity_shares,
                 average_cost_usd=excluded.average_cost_usd,
                 realized_trading_pnl_usd=excluded.realized_trading_pnl_usd,
                 realized_inventory_pnl_usd=excluded.realized_inventory_pnl_usd,
                 fees_usd=excluded.fees_usd,updated_at_utc=excluded.updated_at_utc""",
                (
                    order["token_id"],
                    order["market_id"],
                    order["event_id"],
                    order["outcome"],
                    after,
                    average,
                    prior_trading + realized_trading,
                    prior_inventory,
                    prior_fees + fees_usd,
                    utc_now(),
                ),
            )
        return True

    def record_confirmed_income(
        self,
        *,
        income_key: str,
        date_utc: str,
        market_id: str,
        kind: str,
        amount_usd: float,
        source: str,
        raw: dict[str, Any],
    ) -> None:
        if kind not in {"MAKER_REBATE", "LIQUIDITY_REWARD"} or amount_usd < 0:
            raise ReconciliationError("invalid confirmed income")
        with self.conn:
            self.conn.execute(
                """INSERT OR IGNORE INTO live_confirmed_income
                (income_key,date_utc,market_id,kind,amount_usd,source,raw_json) VALUES(?,?,?,?,?,?,?)""",
                (income_key, date_utc, market_id, kind, amount_usd, source, _json(raw)),
            )

    def daily_realized_net(self, date_utc: str) -> float:
        fills = self.conn.execute(
            """SELECT COALESCE(SUM(realized_trading_pnl_usd+realized_inventory_pnl_usd-fees_usd
            +COALESCE(rebate_amount_usd,0)+COALESCE(liquidity_reward_usd,0)),0)
            FROM live_fills WHERE substr(timestamp_utc,1,10)=?""",
            (date_utc,),
        ).fetchone()[0]
        income = self.conn.execute(
            "SELECT COALESCE(SUM(amount_usd),0) FROM live_confirmed_income WHERE date_utc=?",
            (date_utc,),
        ).fetchone()[0]
        return float(fills) + float(income)

    def attributable_markets(self, date_utc: str, *, require_fill: bool) -> set[str]:
        if require_fill:
            rows = self.conn.execute(
                "SELECT DISTINCT market_id FROM live_fills WHERE substr(timestamp_utc,1,10)=?",
                (date_utc,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT market_id FROM live_orders WHERE substr(submitted_at_utc,1,10)=?",
                (date_utc,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def mark_equity(self, midpoint_by_token: dict[str, float]) -> tuple[float, float]:
        unrealized = 0.0
        for row in self.all_inventory():
            midpoint = midpoint_by_token.get(row["token_id"])
            if midpoint is None:
                raise SafetyStop(f"missing mark for inventory token {row['token_id']}")
            unrealized += (midpoint - float(row["average_cost_usd"])) * float(
                row["quantity_shares"]
            )
        realized = self.realized_net()
        equity = realized + unrealized
        peak_row = self.conn.execute(
            "SELECT MAX(peak_equity_pnl_usd) FROM live_equity"
        ).fetchone()
        peak = max(equity, float(peak_row[0]) if peak_row[0] is not None else equity)
        drawdown = max(0.0, peak - equity)
        with self.conn:
            self.conn.execute(
                """INSERT INTO live_equity(timestamp_utc,realized_net_pnl_usd,
                unrealized_inventory_pnl_usd,equity_pnl_usd,peak_equity_pnl_usd,drawdown_usd)
                VALUES(?,?,?,?,?,?)""",
                (utc_now(), realized, unrealized, equity, peak, drawdown),
            )
        return unrealized, drawdown

    def summary(
        self,
        *,
        midpoint_by_token: dict[str, float],
        elapsed_hours: float,
        capital: float,
    ) -> dict[str, Any]:
        unrealized, _ = self.mark_equity(midpoint_by_token)
        aggregate = self.conn.execute(
            """SELECT COUNT(*),COALESCE(SUM(filled_quantity),0),COALESCE(SUM(notional_usd),0),
            COALESCE(SUM(realized_trading_pnl_usd),0),COALESCE(SUM(fees_usd),0),
            COALESCE(SUM(realized_inventory_pnl_usd),0) FROM live_fills"""
        ).fetchone()
        open_rows = [
            row
            for row in self.open_orders()
            if row["status"] != "DRY_RUN_RESTING"
        ]
        deployed = sum(
            max(0.0, float(row["original_quantity"]) - float(row["filled_quantity"]))
            * float(row["submitted_price"])
            for row in open_rows
            if row["side"] == "BUY"
        ) + sum(
            float(row["quantity_shares"]) * float(row["average_cost_usd"])
            for row in self.all_inventory()
        )
        realized = self.realized_net()
        max_dd = float(
            self.conn.execute(
                "SELECT COALESCE(MAX(drawdown_usd),0) FROM live_equity"
            ).fetchone()[0]
        )
        hours = max(elapsed_hours, 1e-9)
        return {
            "capital allocated": capital,
            "capital currently deployed": deployed,
            "open orders": len(open_rows),
            "fills": int(aggregate[0]),
            "filled notional": float(aggregate[2]),
            "gross spread P/L": float(aggregate[3]),
            "fees": float(aggregate[4]),
            "confirmed maker rebates": float(
                self.conn.execute(
                    "SELECT COALESCE(SUM(rebate_amount_usd),0) FROM live_fills"
                ).fetchone()[0]
            ) + self._income_total("MAKER_REBATE"),
            "confirmed liquidity rewards": self._income_total("LIQUIDITY_REWARD"),
            "inventory P/L": float(aggregate[5]),
            "REALIZED NET P/L": realized,
            "unrealized inventory P/L": unrealized,
            "net $/hour": realized / hours,
            "net $/day run rate": realized / hours * 24.0,
            "max drawdown": max_dd,
            "capital turnover": float(aggregate[2]) / capital if capital else 0.0,
        }


class ProcessLock:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.handle: Any = None

    def __enter__(self) -> Self:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise SafetyStop(
                f"duplicate PARALLAX runner: lock held at {self.path}"
            ) from exc
        self.handle.seek(0)
        self.handle.truncate()
        self.handle.write(str(os.getpid()))
        self.handle.flush()
        return self

    def __exit__(self, *_args: object) -> None:
        if self.handle is not None:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


class PolymarketVenue:
    REQUIRED_ENV = (
        "PARALLAX_POLYMARKET_PRIVATE_KEY",
        "PARALLAX_POLYMARKET_API_KEY",
        "PARALLAX_POLYMARKET_API_SECRET",
        "PARALLAX_POLYMARKET_API_PASSPHRASE",
        "PARALLAX_POLYMARKET_SIGNATURE_TYPE",
    )

    def __init__(self, *, live_enabled: bool) -> None:
        if not live_enabled or os.environ.get(LIVE_ENV) != LIVE_ENABLED_VALUE:
            raise SafetyStop(
                f"live mode refused: {LIVE_ENV} must equal {LIVE_ENABLED_VALUE}"
            )
        missing = [name for name in self.REQUIRED_ENV if not os.environ.get(name)]
        if missing:
            raise SafetyStop(
                f"missing live authentication variables: {', '.join(missing)}"
            )
        try:
            from py_clob_client_v2 import ApiCreds, ClobClient
        except ImportError as exc:
            raise SafetyStop(
                "py-clob-client-v2 is required for live execution"
            ) from exc
        try:
            signature_type = int(os.environ["PARALLAX_POLYMARKET_SIGNATURE_TYPE"])
        except ValueError as exc:
            raise SafetyStop(
                "PARALLAX_POLYMARKET_SIGNATURE_TYPE must be an integer"
            ) from exc
        if signature_type not in {0, 1, 2, 3}:
            raise SafetyStop("unsupported Polymarket signature type")
        funder = os.environ.get("PARALLAX_POLYMARKET_FUNDER_ADDRESS") or None
        if signature_type != 0 and not funder:
            raise SafetyStop(
                "PARALLAX_POLYMARKET_FUNDER_ADDRESS is required for delegated wallets"
            )
        creds = ApiCreds(
            api_key=os.environ["PARALLAX_POLYMARKET_API_KEY"],
            api_secret=os.environ["PARALLAX_POLYMARKET_API_SECRET"],
            api_passphrase=os.environ["PARALLAX_POLYMARKET_API_PASSPHRASE"],
        )
        self.client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=os.environ["PARALLAX_POLYMARKET_PRIVATE_KEY"],
            creds=creds,
            signature_type=signature_type,
            funder=funder,
            use_server_time=True,
        )
        self.address = self.client.get_address()

    def authenticate(self, *, allow_closed_only: bool = False) -> None:
        try:
            health = self.client.get_ok()
            if health is False or (
                isinstance(health, dict) and health.get("ok") is False
            ):
                raise SafetyStop("Polymarket venue health check failed")
            closed_only = self.client.get_closed_only_mode()
            is_closed_only = closed_only is True or (
                isinstance(closed_only, dict)
                and (
                    closed_only.get("closed_only") is True
                    or closed_only.get("closedOnly") is True
                )
            )
            if is_closed_only and not allow_closed_only:
                raise SafetyStop("Polymarket account is in closed-only mode")
            self.client.get_open_orders(only_first_page=True)
        except Exception as exc:
            raise SafetyStop(
                f"Polymarket authentication failed: {type(exc).__name__}: {exc}"
            ) from exc

    def place_post_only(self, intent: OrderIntent) -> dict[str, Any]:
        from py_clob_client_v2 import (
            OrderArgs,
            OrderType,
            PartialCreateOrderOptions,
            Side,
        )

        side = Side.BUY if intent.side == "BUY" else Side.SELL
        return self.client.create_and_post_order(
            OrderArgs(
                token_id=intent.token_id,
                price=intent.price,
                size=intent.size_shares,
                side=side,
            ),
            PartialCreateOrderOptions(tick_size=str(intent.tick_size)),
            order_type=OrderType.GTC,
            post_only=True,
            defer_exec=False,
        )

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        from py_clob_client_v2 import OrderPayload

        return self.client.cancel_order(OrderPayload(orderID=order_id))

    def cancel_all(self) -> dict[str, Any]:
        return self.client.cancel_all()

    def get_open_orders(self) -> list[dict[str, Any]]:
        return self.client.get_open_orders()

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self.client.get_order(order_id)

    def get_trades(self, *, after: int | None = None) -> list[dict[str, Any]]:
        from py_clob_client_v2 import TradeParams

        return self.client.get_trades(TradeParams(after=after) if after else None)

    def heartbeat(self, heartbeat_id: str) -> dict[str, Any]:
        return self.client.post_heartbeat(heartbeat_id)

    def confirmed_rewards(self, date: str) -> list[dict[str, Any]]:
        result = self.client.get_earnings_for_user_for_day(date)
        return result if isinstance(result, list) else list(result.get("data", []))


class ExecutionEngine:
    OPEN_VENUE_STATUSES: ClassVar[set[str]] = {"LIVE", "ORDER_STATUS_LIVE"}

    def __init__(
        self, *, store: LiveStore, venue: Venue | None, limits: LiveLimits, mode: str
    ) -> None:
        if mode not in {"DRY_RUN", "LIVE"}:
            raise ValueError("mode must be DRY_RUN or LIVE")
        if mode == "LIVE" and venue is None:
            raise ValueError("live mode requires an authenticated venue")
        self.store = store
        self.venue = venue
        self.limits = limits
        self.mode = mode
        self.heartbeat_id = ""

    def _open_exposure(self, *, market_id: str | None = None) -> float:
        total = 0.0
        for row in self.store.open_orders():
            if row["side"] != "BUY" or (market_id and row["market_id"] != market_id):
                continue
            total += (
                max(0.0, row["original_quantity"] - row["filled_quantity"])
                * row["submitted_price"]
            )
        inventory = self.store.all_inventory()
        total += sum(
            row["quantity_shares"] * row["average_cost_usd"]
            for row in inventory
            if not market_id or row["market_id"] == market_id
        )
        return float(total)

    def _reserved_sell_shares(self, token_id: str) -> float:
        return sum(
            max(0.0, row["original_quantity"] - row["filled_quantity"])
            for row in self.store.open_orders()
            if row["side"] == "SELL" and row["token_id"] == token_id
        )

    def size_intent(self, ranked: RankedCandidate, *, side: str) -> OrderIntent | None:
        candidate = ranked.candidate
        if not candidate.accepting_orders or not candidate.market_active:
            return None
        price = candidate.best_bid if side == "BUY" else candidate.best_ask
        market_room = self.limits.per_market_usd - self._open_exposure(
            market_id=candidate.market_id
        )
        total_room = self.limits.max_deployed_usd - self._open_exposure()
        if side == "BUY":
            inventory_value = sum(
                float(row["quantity_shares"]) * float(row["average_cost_usd"])
                for row in self.store.all_inventory()
                if row["market_id"] == candidate.market_id
            )
            inventory_room = self.limits.max_inventory_usd_per_market - inventory_value
            budget = min(
                self.limits.per_order_usd, market_room, total_room, inventory_room
            )
            shares = min(ranked.quote_size_shares, max(0.0, budget) / price)
        elif side == "SELL":
            inventory = self.store.inventory(candidate.token_id)
            available = (
                float(inventory["quantity_shares"]) if inventory else 0.0
            ) - self._reserved_sell_shares(candidate.token_id)
            shares = min(max(0.0, available), self.limits.per_order_usd / price)
            if inventory is None or price <= float(inventory["average_cost_usd"]):
                return None
        else:
            raise ValueError("side must be BUY or SELL")
        shares = math.floor(shares * 100) / 100
        if (
            shares + 1e-9 < candidate.min_order_size_shares
            or shares * price > self.limits.per_order_usd + 1e-9
        ):
            return None
        return OrderIntent(
            candidate.market_id,
            candidate.event_id,
            candidate.token_id,
            candidate.outcome,
            side,
            price,
            shares,
            candidate.tick_size,
            ranked.expected_net_usd_per_hour,
        )

    def place(self, intent: OrderIntent) -> str:
        if intent.notional_usd > self.limits.per_order_usd + 1e-9:
            raise SafetyStop("per-order notional cap exceeded")
        if (
            self._open_exposure(market_id=intent.market_id)
            + (intent.notional_usd if intent.side == "BUY" else 0)
            > self.limits.per_market_usd + 1e-9
        ):
            raise SafetyStop("per-market capital cap exceeded")
        if (
            self._open_exposure() + (intent.notional_usd if intent.side == "BUY" else 0)
            > self.limits.max_deployed_usd + 1e-9
        ):
            raise SafetyStop("maximum deployed capital exceeded")
        active = {row["market_id"] for row in self.store.open_orders()} | {
            row["market_id"] for row in self.store.all_inventory()
        }
        if (
            intent.market_id not in active
            and len(active) >= self.limits.max_active_markets
        ):
            raise SafetyStop("maximum simultaneous markets exceeded")
        local_id = self.store.submit_order(intent, mode=self.mode)
        if self.mode == "DRY_RUN":
            return local_id
        assert self.venue is not None
        started = time.monotonic()
        try:
            response = self.venue.place_post_only(intent)
        except Exception as exc:
            self.store.reject_order(local_id, {"error": f"{type(exc).__name__}: {exc}"})
            self.emergency_stop("order submission/authentication failure")
            raise SafetyStop(
                f"order submission failed: {type(exc).__name__}: {exc}"
            ) from exc
        latency = (time.monotonic() - started) * 1000.0
        order_id = str(response.get("orderID") or response.get("order_id") or "")
        status = str(response.get("status") or "").upper()
        success = response.get("success") is True and bool(order_id)
        if not success or status not in self.OPEN_VENUE_STATUSES:
            self.store.reject_order(local_id, response)
            self.emergency_stop("post-only order rejected or did not rest")
            raise SafetyStop(
                f"post-only order did not rest: {response.get('errorMsg') or status or 'unknown'}"
            )
        self.store.acknowledge_order(
            local_id,
            venue_order_id=order_id,
            status="RESTING",
            latency_ms=latency,
            raw=response,
        )
        return order_id

    def cancel(self, order_id: str, reason: str) -> None:
        row = self.store.order_by_venue_id(order_id)
        if row is None:
            raise ReconciliationError(f"cannot cancel unknown local order {order_id}")
        if self.mode == "LIVE":
            assert self.venue is not None
            try:
                response = self.venue.cancel_order(order_id)
                not_cancelled = response.get("not_canceled") or response.get(
                    "notCanceled"
                )
                if not_cancelled and (
                    order_id in not_cancelled
                    if isinstance(not_cancelled, (list, tuple, set, dict))
                    else True
                ):
                    raise ReconciliationError(f"venue did not cancel order {order_id}")
            except Exception as exc:
                self.emergency_stop("single-order cancellation failure")
                raise SafetyStop(
                    f"order cancellation failed: {type(exc).__name__}: {exc}"
                ) from exc
        self.store.set_order_state(
            order_id, "CANCELLED", float(row["filled_quantity"]), reason=reason
        )

    def cancel_stale_or_moved(
        self, candidates: dict[str, Candidate], *, now_monotonic: float
    ) -> int:
        cancelled = 0
        now_utc = datetime.now(UTC)
        for row in list(self.store.open_orders()):
            candidate = candidates.get(row["token_id"])
            if (
                candidate is None
                or not candidate.accepting_orders
                or not candidate.market_active
            ):
                reason = "market unavailable/paused/closed"
            elif (
                now_monotonic - candidate.book_observed_monotonic
                > self.limits.stale_book_seconds
            ):
                reason = "stale book"
            else:
                submitted = datetime.fromisoformat(row["submitted_at_utc"])
                age = (now_utc - submitted).total_seconds()
                current_price = (
                    candidate.best_bid if row["side"] == "BUY" else candidate.best_ask
                )
                ticks = (
                    abs(current_price - float(row["submitted_price"]))
                    / candidate.tick_size
                )
                if age > self.limits.max_quote_age_seconds:
                    reason = "maximum quote age"
                elif ticks + 1e-9 >= self.limits.material_book_move_ticks:
                    reason = "material book move"
                elif (
                    row["side"] == "BUY"
                    and rank_candidate(candidate, self.limits) is None
                ):
                    reason = "expected economics non-positive"
                elif row["side"] == "SELL":
                    inventory = self.store.inventory(candidate.token_id)
                    if (
                        inventory is None
                        or float(inventory["quantity_shares"]) <= 0
                        or candidate.best_ask <= float(inventory["average_cost_usd"])
                    ):
                        reason = "inventory/economics no longer support maker ask"
                    else:
                        continue
                else:
                    continue
            if self.mode == "DRY_RUN":
                self.store.conn.execute(
                    "UPDATE live_orders SET status='CANCELLED',cancel_reason=?,last_update_at_utc=? WHERE local_order_id=?",
                    (reason, utc_now(), row["local_order_id"]),
                )
                self.store.conn.commit()
            else:
                self.cancel(str(row["venue_order_id"]), reason)
            cancelled += 1
        return cancelled

    @staticmethod
    def _timestamp(value: Any) -> str:
        if value in (None, ""):
            raise ReconciliationError("confirmed trade has no timestamp")
        if isinstance(value, str) and ("T" in value or value.endswith("Z")):
            parsed = datetime.fromisoformat(value)
        else:
            stamp = float(value)
            if stamp > 10_000_000_000:
                stamp /= 1000.0
            parsed = datetime.fromtimestamp(stamp, tz=UTC)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")

    def _fill_fragments(
        self, trade: dict[str, Any]
    ) -> list[tuple[str, float, float, float, float]]:
        trade_id = str(trade.get("id") or trade.get("trade_id") or "")
        if not trade_id:
            raise ReconciliationError("confirmed trade has no ID")
        fragments: list[tuple[str, float, float, float, float]] = []
        makers = trade.get("maker_orders") or trade.get("makerOrders") or []
        for maker in makers if isinstance(makers, list) else []:
            if not isinstance(maker, dict):
                continue
            order_id = str(
                maker.get("order_id") or maker.get("orderID") or maker.get("id") or ""
            )
            if not order_id or self.store.order_by_venue_id(order_id) is None:
                continue
            quantity = _float(
                maker.get("matched_amount") or maker.get("size"), name="maker fill size"
            )
            price = _float(
                maker.get("price") or trade.get("price"), name="maker fill price"
            )
            fee = _float(maker.get("fee") or 0, name="maker fill fee")
            rebate = _float(maker.get("rebate") or 0, name="maker fill rebate")
            if fee < 0 or rebate < 0:
                raise ReconciliationError("negative fill fee/rebate")
            fragments.append(
                (f"{trade_id}:{order_id}", quantity, price, fee, rebate)
            )
        if not fragments:
            taker_order = str(trade.get("taker_order_id") or "")
            if taker_order and self.store.order_by_venue_id(taker_order) is not None:
                raise ReconciliationError(
                    "locally submitted post-only order appeared as taker"
                )
        return fragments

    def _record_cumulative_order_fill(
        self, order_id: str, venue_row: dict[str, Any]
    ) -> int:
        """Record an authoritative cumulative fill without duplicating prior deltas."""
        if venue_row.get("cumulative_fill") is not True:
            return 0
        matched = _float(
            venue_row.get("size_matched") or 0, name="cumulative matched order size"
        )
        accounted_row = self.store.conn.execute(
            """SELECT COALESCE(SUM(filled_quantity),0),COALESCE(SUM(notional_usd),0),
            COALESCE(SUM(fees_usd),0),COALESCE(SUM(rebate_amount_usd),0)
            FROM live_fills WHERE order_id=?""",
            (order_id,),
        ).fetchone()
        accounted = float(accounted_row[0])
        if matched + 1e-9 < accounted:
            raise ReconciliationError(
                f"venue cumulative fill regressed for order {order_id}"
            )
        delta_quantity = matched - accounted
        if delta_quantity <= 1e-9:
            return 0
        average_price = _float(
            venue_row.get("avg_fill_price"), name="cumulative average fill price"
        )
        total_notional = matched * average_price
        delta_notional = total_notional - float(accounted_row[1])
        delta_price = delta_notional / delta_quantity
        total_fees = _float(
            venue_row.get("fees_total_usd") or 0, name="cumulative fill fees"
        )
        total_rebate = _float(
            venue_row.get("maker_rebate_total_usd") or 0,
            name="cumulative maker rebate",
        )
        delta_fees = total_fees - float(accounted_row[2])
        delta_rebate = total_rebate - float(accounted_row[3])
        if delta_fees < -1e-9 or delta_rebate < -1e-9:
            raise ReconciliationError(
                f"venue cumulative economics regressed for order {order_id}"
            )
        order = self.store.order_by_venue_id(order_id)
        if order is None:
            raise ReconciliationError(f"cumulative fill references unknown order {order_id}")
        timestamp_value = venue_row.get("timestamp") or utc_now()
        timestamp = self._timestamp(timestamp_value)
        key = f"cumulative:{order_id}:{matched:.12g}"
        return int(
            self.store.record_fill(
                external_fill_key=key,
                order=order,
                timestamp_utc=timestamp,
                fill_price=delta_price,
                quantity=delta_quantity,
                fees_usd=max(0.0, delta_fees),
                rebate_amount_usd=max(0.0, delta_rebate),
                raw=venue_row.get("raw")
                if isinstance(venue_row.get("raw"), dict)
                else venue_row,
            )
        )

    def reconcile(self) -> int:
        if self.mode == "DRY_RUN":
            return 0
        assert self.venue is not None
        try:
            venue_open = self.venue.get_open_orders()
            trades = self.venue.get_trades()
        except Exception as exc:
            self.emergency_stop("cannot retrieve authoritative order/fill state")
            raise ReconciliationError(
                f"venue reconciliation retrieval failed: {exc}"
            ) from exc
        unresolved_submissions = [
            row for row in self.store.open_orders() if not row["venue_order_id"]
        ]
        if unresolved_submissions:
            self.emergency_stop("submitted order has no authoritative venue ID")
            raise ReconciliationError("local submitted order has unknown venue state")
        open_by_id = {
            str(row.get("id") or row.get("orderID") or row.get("order_id")): row
            for row in venue_open
            if isinstance(row, dict)
        }
        local_open = {
            str(row["venue_order_id"]): row
            for row in self.store.open_orders()
            if row["venue_order_id"]
        }
        unknown = set(open_by_id) - set(local_open)
        if unknown:
            self.emergency_stop("venue contains open orders not owned by this ledger")
            raise ReconciliationError(f"unknown venue open orders: {sorted(unknown)}")
        fills_added = 0
        for trade in trades:
            if not isinstance(trade, dict):
                raise ReconciliationError("malformed venue trade response")
            status = str(trade.get("status") or "").upper()
            if status not in {
                "CONFIRMED",
                "TRADE_STATUS_CONFIRMED",
                "MATCHED",
                "MINED",
            }:
                continue
            timestamp = self._timestamp(
                trade.get("match_time")
                or trade.get("timestamp")
                or trade.get("created_at")
            )
            for key, quantity, price, fee, rebate in self._fill_fragments(trade):
                order_id = key.split(":", 1)[1]
                order = self.store.order_by_venue_id(order_id)
                assert order is not None
                if self.store.record_fill(
                    external_fill_key=key,
                    order=order,
                    timestamp_utc=timestamp,
                    fill_price=price,
                    quantity=quantity,
                    fees_usd=fee,
                    rebate_amount_usd=rebate,
                    raw=trade,
                ):
                    fills_added += 1
        for order_id, row in local_open.items():
            venue_row = open_by_id.get(order_id)
            if venue_row is not None:
                fills_added += self._record_cumulative_order_fill(order_id, venue_row)
                matched = _float(
                    venue_row.get("size_matched") or venue_row.get("matched_size") or 0,
                    name="matched order size",
                )
                accounted = float(
                    self.store.conn.execute(
                        "SELECT COALESCE(SUM(filled_quantity),0) FROM live_fills WHERE order_id=?",
                        (order_id,),
                    ).fetchone()[0]
                )
                if matched > accounted + 1e-9:
                    self.emergency_stop(
                        "venue reports a fill absent from confirmed trade ledger"
                    )
                    raise ReconciliationError(
                        f"unknown fill state for order {order_id}"
                    )
                status = "PARTIALLY_FILLED" if matched > 0 else "RESTING"
                self.store.set_order_state(order_id, status, matched)
                continue
            try:
                terminal = self.venue.get_order(order_id)
            except Exception as exc:
                self.emergency_stop("missing local order cannot be resolved")
                raise ReconciliationError(
                    f"unknown state for order {order_id}: {exc}"
                ) from exc
            status = str(
                terminal.get("status") or terminal.get("order_status") or ""
            ).upper()
            fills_added += self._record_cumulative_order_fill(order_id, terminal)
            matched = _float(
                terminal.get("size_matched") or terminal.get("matched_size") or 0,
                name="terminal matched size",
            )
            accounted = float(
                self.store.conn.execute(
                    "SELECT COALESCE(SUM(filled_quantity),0) FROM live_fills WHERE order_id=?",
                    (order_id,),
                ).fetchone()[0]
            )
            if matched > accounted + 1e-9:
                self.emergency_stop(
                    "terminal order fill absent from confirmed trade ledger"
                )
                raise ReconciliationError(
                    f"unknown terminal fill state for order {order_id}"
                )
            if status in {
                "CANCELLED",
                "CANCELED",
                "ORDER_STATUS_CANCELLED",
                "ORDER_STATUS_CANCELED",
            }:
                local_status = "CANCELLED"
            elif matched + 1e-9 >= float(row["original_quantity"]) or status in {
                "MATCHED",
                "FILLED",
                "ORDER_STATUS_MATCHED",
            }:
                local_status = "FILLED"
            else:
                self.emergency_stop("venue order disappeared without terminal state")
                raise ReconciliationError(
                    f"non-terminal missing order {order_id}: {status}"
                )
            self.store.set_order_state(order_id, local_status, matched)
        return fills_added

    def check_risk(
        self,
        *,
        midpoint_by_token: dict[str, float],
        websocket_last_message_monotonic: float,
        websocket_failed: bool,
        now_monotonic: float,
    ) -> None:
        kill_file = Path(os.environ.get(KILL_FILE_ENV, "data/parallax.kill"))
        if os.environ.get(KILL_ENV) == KILL_SWITCH_VALUE or kill_file.exists():
            self.emergency_stop("global kill switch")
            raise SafetyStop("global kill switch is active")
        if (
            websocket_failed
            or now_monotonic - websocket_last_message_monotonic
            > self.limits.stale_websocket_seconds
        ):
            self.emergency_stop("websocket stale/disconnected")
            raise SafetyStop("market websocket is stale or disconnected")
        unrealized, drawdown = self.store.mark_equity(midpoint_by_token)
        daily = self.store.daily_realized_net(datetime.now(UTC).date().isoformat())
        if daily <= -self.limits.max_daily_loss_usd:
            self.emergency_stop("daily realized loss cap")
            raise SafetyStop("daily realized loss cap reached")
        if drawdown >= self.limits.max_drawdown_usd:
            self.emergency_stop("total drawdown cap")
            raise SafetyStop("total drawdown cap reached")
        inventory_value_by_market: dict[str, float] = {}
        for inventory in self.store.all_inventory():
            mark = midpoint_by_token.get(inventory["token_id"])
            if mark is None:
                self.emergency_stop("inventory mark unavailable")
                raise SafetyStop("inventory state cannot be marked")
            market_id = str(inventory["market_id"])
            inventory_value_by_market[market_id] = (
                inventory_value_by_market.get(market_id, 0.0)
                + inventory["quantity_shares"] * mark
            )
        if any(
            value > self.limits.max_inventory_usd_per_market + 1e-9
            for value in inventory_value_by_market.values()
        ):
            self.emergency_stop("inventory cap violation")
            raise SafetyStop("inventory cap violated")
        if self._open_exposure() > self.limits.max_deployed_usd + 1e-9:
            self.emergency_stop("deployed capital cap violation")
            raise SafetyStop("deployed capital cap violated")
        _ = unrealized

    def send_heartbeat(self) -> None:
        if self.mode == "DRY_RUN":
            return
        assert self.venue is not None
        try:
            response = self.venue.heartbeat(self.heartbeat_id)
            next_id = str(
                response.get("heartbeat_id") or response.get("heartbeatId") or ""
            )
            if not next_id:
                raise ValueError("heartbeat response omitted next ID")
            self.heartbeat_id = next_id
        except Exception as exc:
            self.emergency_stop("venue heartbeat failure")
            raise SafetyStop(f"venue heartbeat failed: {exc}") from exc

    def emergency_stop(self, reason: str) -> None:
        self.store.event("EMERGENCY_STOP", {"reason": reason})
        if self.mode == "LIVE" and self.venue is not None:
            try:
                response = self.venue.cancel_all()
                not_cancelled = response.get("not_canceled") or response.get(
                    "notCanceled"
                )
                remaining = self.venue.get_open_orders()
                if not_cancelled or remaining:
                    raise ReconciliationError(
                        f"cancel-all left venue orders open: {not_cancelled or remaining}"
                    )
            except Exception as exc:
                self.store.event(
                    "CANCEL_ALL_FAILED",
                    {"reason": reason, "error": f"{type(exc).__name__}: {exc}"},
                )
                raise SafetyStop(
                    f"CRITICAL: global cancel-all failed after {reason}: {exc}"
                ) from exc
        for row in self.store.open_orders():
            if row["venue_order_id"]:
                self.store.set_order_state(
                    str(row["venue_order_id"]),
                    "CANCELLED",
                    float(row["filled_quantity"]),
                    reason=reason,
                )
            else:
                self.store.conn.execute(
                    "UPDATE live_orders SET status='CANCELLED',cancel_reason=? WHERE local_order_id=?",
                    (reason, row["local_order_id"]),
                )
        self.store.conn.commit()
