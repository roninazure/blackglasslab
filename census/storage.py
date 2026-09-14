from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS opportunity_episodes (
  opportunity_key TEXT PRIMARY KEY,
  alpha_engine TEXT NOT NULL, market_id TEXT NOT NULL, event_id TEXT,
  started_at_utc TEXT NOT NULL, ended_at_utc TEXT NOT NULL,
  start_monotonic_ns INTEGER NOT NULL, end_monotonic_ns INTEGER NOT NULL,
  lifetime_seconds REAL NOT NULL, observed_executable_seconds REAL,
  best_bid REAL, best_ask REAL, best_spread REAL,
  worst_spread REAL, best_executable_depth_usd REAL, worst_executable_depth_usd REAL,
  gross_edge_usd REAL, fee_source TEXT NOT NULL, fee_rate REAL,
  maker_rebate_rate REAL, maker_rebate_economics TEXT NOT NULL,
  net_executable_edge_usd REAL, slippage_source TEXT NOT NULL,
  executable INTEGER NOT NULL, theoretical INTEGER NOT NULL, book_valid INTEGER,
  execution_validation_status TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',
  durability_basis TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN',
  survives_1s INTEGER, survives_5s INTEGER, survives_30s INTEGER, survives_60s INTEGER,
  durability_unknown_reason TEXT, quote_count INTEGER NOT NULL,
  quote_movement_json TEXT NOT NULL, adverse_selection_proxy_json TEXT NOT NULL,
  rejection_reason TEXT, metadata_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_episode_engine ON opportunity_episodes(alpha_engine, started_at_utc);
CREATE TABLE IF NOT EXISTS coverage (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at_utc TEXT NOT NULL,
  sampling_mode TEXT NOT NULL, requested_events INTEGER, observed_events INTEGER,
  observed_markets INTEGER, observed_tokens INTEGER, exclusion_reason TEXT,
  metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS engine_coverage (
  alpha_engine TEXT PRIMARY KEY, eligible_markets INTEGER NOT NULL,
  tracked_markets INTEGER NOT NULL, opportunity_episodes INTEGER NOT NULL,
  supported INTEGER NOT NULL, unsupported_reason TEXT
);
CREATE TABLE IF NOT EXISTS neg_risk_validation (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at_utc TEXT NOT NULL,
  event_id TEXT NOT NULL, candidate_basket INTEGER NOT NULL, validated_basket INTEGER NOT NULL,
  rejection_reason TEXT, executable_simultaneous_depth_usd REAL,
  gross_structural_edge_usd REAL, net_economics_status TEXT NOT NULL, metadata_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS stream_health (
  id INTEGER PRIMARY KEY CHECK (id=1), started_at_utc TEXT NOT NULL, stopped_at_utc TEXT,
  connection_count INTEGER NOT NULL, reconnect_count INTEGER NOT NULL,
  disconnect_count INTEGER NOT NULL, protocol_error_count INTEGER NOT NULL,
  error_count INTEGER NOT NULL, last_message_at_utc TEXT, messages INTEGER NOT NULL,
  messages_per_second REAL, stale_stream_events INTEGER NOT NULL,
  max_recovery_seconds REAL, duration_seconds REAL, automatic_shutdown INTEGER NOT NULL,
  details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revenue_control (
  id INTEGER PRIMARY KEY CHECK (id=1), captured_at_utc TEXT NOT NULL,
  production_db_path TEXT NOT NULL, position_count INTEGER NOT NULL, horizon_mix_json TEXT NOT NULL,
  realized_pnl_usd REAL NOT NULL, unrealized_pnl_usd REAL NOT NULL,
  deployed_capital_usd REAL NOT NULL, opportunity_count INTEGER NOT NULL,
  admission_count INTEGER NOT NULL, status TEXT NOT NULL, error TEXT
);
CREATE TABLE IF NOT EXISTS resource_telemetry (
  id INTEGER PRIMARY KEY CHECK (id=1), captured_at_utc TEXT NOT NULL,
  cpu_user_seconds REAL NOT NULL, cpu_system_seconds REAL NOT NULL, max_rss_bytes INTEGER NOT NULL,
  db_bytes INTEGER NOT NULL, wal_bytes INTEGER NOT NULL, persisted_episodes INTEGER NOT NULL,
  episodes_per_minute REAL NOT NULL, production_freshness_seconds REAL, details_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production_cycle_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT, captured_at_utc TEXT NOT NULL,
  cycle_id TEXT, cycle_timestamp_utc TEXT, freshness_seconds REAL,
  runner_pid INTEGER, runner_state TEXT NOT NULL, cycle_state TEXT NOT NULL,
  warning TEXT, source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, recorded_at_utc TEXT NOT NULL,
  event_type TEXT NOT NULL, detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS collector_status (
  id INTEGER PRIMARY KEY CHECK (id=1),
  status TEXT NOT NULL CHECK (status IN ('STARTING','RUNNING','FAILED','STOPPED')),
  phase TEXT NOT NULL, started_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL,
  running_at_utc TEXT, stopped_at_utc TEXT, error TEXT
);
"""


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def observation_key(row: dict[str, Any]) -> str:
    return hashlib.sha256(_json(row).encode("utf-8")).hexdigest()


class CensusStore:
    """Sparse, census-only store. It never opens the production DB."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate_episode_semantics()
        self.conn.commit()

    def _migrate_episode_semantics(self) -> None:
        """Add explicit semantics without rewriting legacy census evidence."""
        columns = {
            str(row[1])
            for row in self.conn.execute("PRAGMA table_info(opportunity_episodes)")
        }
        additions = {
            "observed_executable_seconds": "REAL",
            "book_valid": "INTEGER",
            "execution_validation_status": (
                "TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN'"
            ),
            "durability_basis": "TEXT NOT NULL DEFAULT 'LEGACY_UNKNOWN'",
        }
        for name, declaration in additions.items():
            if name not in columns:
                self.conn.execute(
                    f"ALTER TABLE opportunity_episodes ADD COLUMN {name} {declaration}"
                )

    def event(self, recorded_at_utc: str, event_type: str, detail: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO collector_events(recorded_at_utc,event_type,detail_json) VALUES(?,?,?)", (recorded_at_utc, event_type, _json(detail)))

    def initialize_status(self, started_at_utc: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO collector_status(id,status,phase,started_at_utc,updated_at_utc,running_at_utc,stopped_at_utc,error) VALUES(1,'STARTING','initializing',?,?,NULL,NULL,NULL)",
            (started_at_utc, started_at_utc),
        )

    def update_status(self, status: str, phase: str, updated_at_utc: str, *, error: str | None = None) -> None:
        running_at = updated_at_utc if status == "RUNNING" else None
        stopped_at = updated_at_utc if status in {"FAILED", "STOPPED"} else None
        self.conn.execute(
            """UPDATE collector_status
               SET status=?,phase=?,updated_at_utc=?,
                   running_at_utc=COALESCE(running_at_utc,?),
                   stopped_at_utc=COALESCE(?,stopped_at_utc),error=?
               WHERE id=1""",
            (status, phase, updated_at_utc, running_at, stopped_at, error),
        )

    def record_coverage(self, recorded_at_utc: str, **values: Any) -> None:
        self.conn.execute("INSERT INTO coverage(recorded_at_utc,sampling_mode,requested_events,observed_events,observed_markets,observed_tokens,exclusion_reason,metadata_json) VALUES(?,?,?,?,?,?,?,?)", (recorded_at_utc, values.get("sampling_mode", "UNKNOWN"), values.get("requested_events"), values.get("observed_events"), values.get("observed_markets"), values.get("observed_tokens"), values.get("exclusion_reason"), _json(values.get("metadata", {}))))

    def record_episode(self, state: Any, *, engine: str, market_id: str, event_id: str | None, ended_at_utc: str, metadata: dict[str, Any]) -> None:
        if engine != "negrisk_structural" and state.economic_executable is not None:
            raise ValueError(
                "economic executability must remain UNKNOWN for unvalidated engines"
            )
        validated_executable = int(
            engine == "negrisk_structural"
            and state.economic_executable is True
            and state.execution_validation_status == "VALIDATED_EXECUTABLE"
        )
        self.conn.execute(
            """INSERT OR REPLACE INTO opportunity_episodes
            (opportunity_key,alpha_engine,market_id,event_id,started_at_utc,ended_at_utc,start_monotonic_ns,end_monotonic_ns,lifetime_seconds,observed_executable_seconds,best_bid,best_ask,best_spread,worst_spread,best_executable_depth_usd,worst_executable_depth_usd,gross_edge_usd,fee_source,fee_rate,maker_rebate_rate,maker_rebate_economics,net_executable_edge_usd,slippage_source,executable,theoretical,book_valid,execution_validation_status,durability_basis,survives_1s,survives_5s,survives_30s,survives_60s,durability_unknown_reason,quote_count,quote_movement_json,adverse_selection_proxy_json,rejection_reason,metadata_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (state.key, engine, market_id, event_id, state.started_at_utc, ended_at_utc, state.started_ns, state.end_ns, state.lifetime_seconds, state.observed_executable_seconds, state.best_bid, state.best_ask, state.best_spread, state.worst_spread, state.best_depth_usd, state.worst_depth_usd, state.gross_edge_usd, state.fee_source, state.fee_rate, state.maker_rebate_rate, state.maker_rebate_economics, state.net_edge_usd, state.slippage_source, validated_executable, int(state.book_valid), int(state.book_valid), state.execution_validation_status, state.durability_basis, state.checkpoints[1.0], state.checkpoints[5.0], state.checkpoints[30.0], state.checkpoints[60.0], state.unknown_reason, state.quote_count, _json(state.quote_movements), _json(state.adverse_selection), state.rejection_reason, _json(metadata)),
        )

    def record_engine_coverage(self, rows: list[dict[str, Any]]) -> None:
        self.conn.executemany("INSERT OR REPLACE INTO engine_coverage(alpha_engine,eligible_markets,tracked_markets,opportunity_episodes,supported,unsupported_reason) VALUES(?,?,?,?,?,?)", [(r["alpha_engine"], r["eligible_markets"], r["tracked_markets"], r["opportunity_episodes"], r["supported"], r.get("unsupported_reason")) for r in rows])

    def record_neg_risk(self, recorded_at_utc: str, **row: Any) -> None:
        self.conn.execute("INSERT INTO neg_risk_validation(recorded_at_utc,event_id,candidate_basket,validated_basket,rejection_reason,executable_simultaneous_depth_usd,gross_structural_edge_usd,net_economics_status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)", (recorded_at_utc, row["event_id"], row["candidate_basket"], row["validated_basket"], row.get("rejection_reason"), row.get("executable_simultaneous_depth_usd"), row.get("gross_structural_edge_usd"), row.get("net_economics_status", "UNKNOWN"), _json(row.get("metadata", {}))))

    def record_stream_health(self, row: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO stream_health(id,started_at_utc,stopped_at_utc,connection_count,reconnect_count,disconnect_count,protocol_error_count,error_count,last_message_at_utc,messages,messages_per_second,stale_stream_events,max_recovery_seconds,duration_seconds,automatic_shutdown,details_json) VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (row["started_at_utc"], row.get("stopped_at_utc"), row["connection_count"], row["reconnect_count"], row["disconnect_count"], row["protocol_error_count"], row["error_count"], row.get("last_message_at_utc"), row["messages"], row.get("messages_per_second"), row["stale_stream_events"], row.get("max_recovery_seconds"), row.get("duration_seconds"), row["automatic_shutdown"], _json(row.get("details", {}))))

    def record_revenue_control(self, row: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO revenue_control(id,captured_at_utc,production_db_path,position_count,horizon_mix_json,realized_pnl_usd,unrealized_pnl_usd,deployed_capital_usd,opportunity_count,admission_count,status,error) VALUES(1,?,?,?,?,?,?,?,?,?,?,?)", (row["captured_at_utc"], row["production_db_path"], row["position_count"], _json(row["horizon_mix"]), row["realized_pnl_usd"], row["unrealized_pnl_usd"], row["deployed_capital_usd"], row["opportunity_count"], row["admission_count"], row["status"], row.get("error")))

    def record_resources(self, row: dict[str, Any]) -> None:
        self.conn.execute("INSERT OR REPLACE INTO resource_telemetry(id,captured_at_utc,cpu_user_seconds,cpu_system_seconds,max_rss_bytes,db_bytes,wal_bytes,persisted_episodes,episodes_per_minute,production_freshness_seconds,details_json) VALUES(1,?,?,?,?,?,?,?,?,?,?)", (row["captured_at_utc"], row["cpu_user_seconds"], row["cpu_system_seconds"], row["max_rss_bytes"], row["db_bytes"], row["wal_bytes"], row["persisted_episodes"], row["episodes_per_minute"], row.get("production_freshness_seconds"), _json(row.get("details", {}))))

    def record_production_cycle(self, row: dict[str, Any]) -> None:
        self.conn.execute("INSERT INTO production_cycle_snapshots(captured_at_utc,cycle_id,cycle_timestamp_utc,freshness_seconds,runner_pid,runner_state,cycle_state,warning,source) VALUES(?,?,?,?,?,?,?,?,?)", (row["captured_at_utc"], row.get("cycle_id"), row.get("cycle_timestamp_utc"), row.get("freshness_seconds"), row.get("runner_pid"), row["runner_state"], row["cycle_state"], row.get("warning"), row["source"]))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
