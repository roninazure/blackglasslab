from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .bls import ReleaseEvidence, SourceAttempt
from .core import MarketBundle

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS trial_runs (
  id INTEGER PRIMARY KEY,
  mode TEXT NOT NULL CHECK(mode IN ('REHEARSAL','LIVE_OBSERVATION')),
  status TEXT NOT NULL,
  started_wall_time_ns INTEGER NOT NULL,
  started_monotonic_ns INTEGER NOT NULL,
  started_iso_utc TEXT NOT NULL,
  event_date TEXT NOT NULL,
  reference_year INTEGER NOT NULL,
  reference_month INTEGER NOT NULL,
  event_id TEXT NOT NULL,
  event_slug TEXT NOT NULL,
  event_title TEXT NOT NULL,
  resolution_source TEXT NOT NULL,
  rules_text TEXT NOT NULL,
  subscribed_assets_json TEXT NOT NULL,
  slippage_bps REAL NOT NULL,
  error TEXT
);
CREATE TABLE IF NOT EXISTS release_evidence (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES trial_runs(id),
  is_rehearsal INTEGER NOT NULL CHECK(is_rehearsal IN (0,1)),
  source_url TEXT NOT NULL,
  raw_payload BLOB NOT NULL,
  payload_sha256 TEXT NOT NULL,
  receipt_wall_time_ns INTEGER NOT NULL,
  receipt_monotonic_ns INTEGER NOT NULL,
  receipt_iso_utc TEXT NOT NULL,
  published_at_utc TEXT NOT NULL,
  entry_id TEXT NOT NULL,
  release_url TEXT NOT NULL,
  reference_year INTEGER NOT NULL,
  reference_month INTEGER NOT NULL,
  change_jobs INTEGER NOT NULL,
  winning_bracket TEXT NOT NULL,
  provenance_text TEXT NOT NULL,
  source_name TEXT,
  valid_wall_time_ns INTEGER,
  valid_monotonic_ns INTEGER
);
CREATE TABLE IF NOT EXISTS arm_precheck (
  run_id INTEGER PRIMARY KEY REFERENCES trial_runs(id),
  local_utc TEXT NOT NULL,
  wall_time_ns INTEGER NOT NULL,
  monotonic_ns INTEGER NOT NULL,
  hostname TEXT NOT NULL,
  platform TEXT NOT NULL,
  clock_sync_status TEXT NOT NULL,
  flash_contact_configured INTEGER NOT NULL CHECK(flash_contact_configured IN (0,1))
);
CREATE TABLE IF NOT EXISTS readiness_checks (
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES trial_runs(id),
  session_id TEXT NOT NULL,
  gate_name TEXT NOT NULL,
  passed INTEGER NOT NULL CHECK(passed IN (0,1)),
  detail TEXT NOT NULL,
  wall_time_ns INTEGER NOT NULL,
  monotonic_ns INTEGER NOT NULL,
  iso_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flash_readiness_session
ON readiness_checks(session_id,id);
CREATE TABLE IF NOT EXISTS source_attempts (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES trial_runs(id),
  phase TEXT NOT NULL,
  source_name TEXT NOT NULL,
  source_url TEXT NOT NULL,
  request_number INTEGER NOT NULL,
  request_started_wall_ns INTEGER NOT NULL,
  request_started_monotonic_ns INTEGER NOT NULL,
  first_byte_wall_ns INTEGER,
  first_byte_monotonic_ns INTEGER,
  body_complete_wall_ns INTEGER NOT NULL,
  body_complete_monotonic_ns INTEGER NOT NULL,
  parse_complete_wall_ns INTEGER NOT NULL,
  parse_complete_monotonic_ns INTEGER NOT NULL,
  parser_runtime_us REAL NOT NULL,
  http_status INTEGER,
  http_date TEXT,
  age TEXT,
  etag TEXT,
  last_modified TEXT,
  cache_control TEXT,
  payload BLOB NOT NULL,
  payload_length INTEGER NOT NULL,
  payload_sha256 TEXT NOT NULL,
  parsed_value INTEGER,
  reference_year INTEGER,
  reference_month INTEGER,
  validation_result TEXT NOT NULL,
  rejection_reason TEXT,
  UNIQUE(run_id,source_name,request_number)
);
CREATE INDEX IF NOT EXISTS idx_flash_source_attempts_run_source ON source_attempts(run_id,source_name,request_number);
CREATE TABLE IF NOT EXISTS market_observations (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES trial_runs(id),
  phase TEXT NOT NULL,
  wall_time_ns INTEGER NOT NULL,
  monotonic_ns INTEGER NOT NULL,
  iso_utc TEXT NOT NULL,
  asset_token TEXT NOT NULL,
  market_id TEXT NOT NULL,
  bracket TEXT NOT NULL,
  token_outcome TEXT NOT NULL,
  bid REAL,
  ask REAL,
  bid_size REAL NOT NULL,
  ask_size REAL NOT NULL,
  known_payout REAL,
  execution_side TEXT,
  price REAL,
  available_shares REAL,
  capital_required REAL,
  gross_edge_per_share REAL,
  fees REAL,
  conservative_slippage REAL,
  net_edge_per_share REAL,
  net_executable_dollars REAL,
  return_on_capital REAL,
  reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_flash_observations_run_mono ON market_observations(run_id, monotonic_ns);
CREATE TABLE IF NOT EXISTS paper_executions (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES trial_runs(id),
  is_rehearsal INTEGER NOT NULL CHECK(is_rehearsal IN (0,1)),
  token TEXT NOT NULL,
  market_id TEXT NOT NULL,
  bracket TEXT NOT NULL,
  side TEXT NOT NULL,
  decision_wall_time_ns INTEGER NOT NULL,
  decision_monotonic_ns INTEGER NOT NULL,
  decision_iso_utc TEXT NOT NULL,
  execution_wall_time_ns INTEGER NOT NULL,
  execution_monotonic_ns INTEGER NOT NULL,
  execution_iso_utc TEXT NOT NULL,
  price REAL NOT NULL,
  shares REAL NOT NULL,
  displayed_shares REAL NOT NULL,
  capital REAL NOT NULL,
  gross_dollars REAL NOT NULL,
  costs REAL NOT NULL,
  net_dollars REAL NOT NULL,
  return_on_capital REAL NOT NULL,
  reason TEXT NOT NULL,
  execution_valid INTEGER NOT NULL DEFAULT 1 CHECK(execution_valid IN (0,1))
);
CREATE TABLE IF NOT EXISTS trial_summary (
  run_id INTEGER PRIMARY KEY REFERENCES trial_runs(id),
  is_rehearsal INTEGER NOT NULL CHECK(is_rehearsal IN (0,1)),
  winning_bracket TEXT,
  t0_wall_time_ns INTEGER,
  t0_monotonic_ns INTEGER,
  t1_wall_time_ns INTEGER,
  t1_monotonic_ns INTEGER,
  t2_wall_time_ns INTEGER,
  t2_monotonic_ns INTEGER,
  t3_wall_time_ns INTEGER,
  t3_monotonic_ns INTEGER,
  source_to_decision_ms REAL,
  source_to_first_market_move_ms REAL,
  profitable_window_ms REAL,
  maximum_executable_stale_depth_shares REAL NOT NULL,
  maximum_deployable_capital REAL NOT NULL,
  maximum_modeled_net_profit REAL NOT NULL,
  complete_books INTEGER NOT NULL,
  stream_stats_json TEXT NOT NULL,
  limitation TEXT,
  winning_source TEXT,
  source_value INTEGER,
  rss_valid_at TEXT,
  summary_valid_at TEXT,
  table_b1_valid_at TEXT,
  rss_minus_winner_ms REAL,
  summary_minus_winner_ms REAL,
  table_b1_minus_winner_ms REAL,
  confirmation_count INTEGER,
  official_source_conflict INTEGER,
  execution_validity INTEGER,
  source_valid_to_resolution_us REAL,
  resolution_to_economics_us REAL,
  source_valid_to_economics_us REAL,
  request_count_rss INTEGER,
  request_count_summary INTEGER,
  request_count_table_b1 INTEGER,
  t0_source_valid_wall_ns INTEGER,
  t0_source_valid_monotonic_ns INTEGER,
  t1_resolution_wall_ns INTEGER,
  t1_resolution_monotonic_ns INTEGER,
  t2_economics_wall_ns INTEGER,
  t2_economics_monotonic_ns INTEGER,
  economics_timing_json TEXT
);
"""

SUMMARY_COLUMNS = (
    "run_id",
    "is_rehearsal",
    "winning_bracket",
    "t0_wall_time_ns",
    "t0_monotonic_ns",
    "t1_wall_time_ns",
    "t1_monotonic_ns",
    "t2_wall_time_ns",
    "t2_monotonic_ns",
    "t3_wall_time_ns",
    "t3_monotonic_ns",
    "source_to_decision_ms",
    "source_to_first_market_move_ms",
    "profitable_window_ms",
    "maximum_executable_stale_depth_shares",
    "maximum_deployable_capital",
    "maximum_modeled_net_profit",
    "complete_books",
    "stream_stats_json",
    "limitation",
    "winning_source",
    "source_value",
    "rss_valid_at",
    "summary_valid_at",
    "table_b1_valid_at",
    "rss_minus_winner_ms",
    "summary_minus_winner_ms",
    "table_b1_minus_winner_ms",
    "confirmation_count",
    "official_source_conflict",
    "execution_validity",
    "source_valid_to_resolution_us",
    "resolution_to_economics_us",
    "source_valid_to_economics_us",
    "request_count_rss",
    "request_count_summary",
    "request_count_table_b1",
    "t0_source_valid_wall_ns",
    "t0_source_valid_monotonic_ns",
    "t1_resolution_wall_ns",
    "t1_resolution_monotonic_ns",
    "t2_economics_wall_ns",
    "t2_economics_monotonic_ns",
    "economics_timing_json",
)


class TrialStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self._ensure_additive_columns()

    def _ensure_column(self, table: str, name: str, declaration: str) -> None:
        columns = {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}
        if name not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")

    def _ensure_additive_columns(self) -> None:
        for name, declaration in (
            ("source_name", "TEXT"),
            ("valid_wall_time_ns", "INTEGER"),
            ("valid_monotonic_ns", "INTEGER"),
        ):
            self._ensure_column("release_evidence", name, declaration)
        self._ensure_column(
            "paper_executions", "execution_valid", "INTEGER NOT NULL DEFAULT 1"
        )
        declarations = {
            "winning_source": "TEXT",
            "source_value": "INTEGER",
            "rss_valid_at": "TEXT",
            "summary_valid_at": "TEXT",
            "table_b1_valid_at": "TEXT",
            "rss_minus_winner_ms": "REAL",
            "summary_minus_winner_ms": "REAL",
            "table_b1_minus_winner_ms": "REAL",
            "confirmation_count": "INTEGER",
            "official_source_conflict": "INTEGER",
            "execution_validity": "INTEGER",
            "source_valid_to_resolution_us": "REAL",
            "resolution_to_economics_us": "REAL",
            "source_valid_to_economics_us": "REAL",
            "request_count_rss": "INTEGER",
            "request_count_summary": "INTEGER",
            "request_count_table_b1": "INTEGER",
            "t0_source_valid_wall_ns": "INTEGER",
            "t0_source_valid_monotonic_ns": "INTEGER",
            "t1_resolution_wall_ns": "INTEGER",
            "t1_resolution_monotonic_ns": "INTEGER",
            "t2_economics_wall_ns": "INTEGER",
            "t2_economics_monotonic_ns": "INTEGER",
            "economics_timing_json": "TEXT",
        }
        for name, declaration in declarations.items():
            self._ensure_column("trial_summary", name, declaration)
        self.conn.commit()

    def start_run(
        self,
        *,
        mode: str,
        wall_ns: int,
        mono_ns: int,
        iso_utc: str,
        event_date: str,
        reference_year: int,
        reference_month: int,
        bundle: MarketBundle,
        slippage_bps: float,
    ) -> int:
        cursor = self.conn.execute(
            """INSERT INTO trial_runs
            (mode,status,started_wall_time_ns,started_monotonic_ns,started_iso_utc,event_date,
             reference_year,reference_month,event_id,event_slug,event_title,resolution_source,
             rules_text,subscribed_assets_json,slippage_bps)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mode,
                "RUNNING",
                wall_ns,
                mono_ns,
                iso_utc,
                event_date,
                reference_year,
                reference_month,
                bundle.event_id,
                bundle.event_slug,
                bundle.title,
                bundle.resolution_source,
                bundle.rules,
                json.dumps(bundle.assets),
                slippage_bps,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def readiness_check(
        self,
        *,
        session_id: str,
        gate_name: str,
        passed: bool,
        detail: str,
        wall_ns: int,
        mono_ns: int,
        iso_utc: str,
        run_id: int | None = None,
    ) -> None:
        self.conn.execute(
            """INSERT INTO readiness_checks
            (run_id,session_id,gate_name,passed,detail,wall_time_ns,monotonic_ns,iso_utc)
            VALUES (?,?,?,?,?,?,?,?)""",
            (
                run_id,
                session_id,
                gate_name,
                int(passed),
                detail,
                wall_ns,
                mono_ns,
                iso_utc,
            ),
        )
        self.conn.commit()

    def release(
        self, run_id: int, evidence: ReleaseEvidence, *, rehearsal: bool, winner: str
    ) -> None:
        self.conn.execute(
            """INSERT INTO release_evidence
            (run_id,is_rehearsal,source_url,raw_payload,payload_sha256,receipt_wall_time_ns,
             receipt_monotonic_ns,receipt_iso_utc,published_at_utc,entry_id,release_url,
             reference_year,reference_month,change_jobs,winning_bracket,provenance_text,
             source_name,valid_wall_time_ns,valid_monotonic_ns)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                int(rehearsal),
                evidence.source_url,
                evidence.payload,
                evidence.payload_sha256,
                evidence.receipt_wall_time_ns,
                evidence.receipt_monotonic_ns,
                evidence.receipt_iso_utc,
                evidence.published_at_utc,
                evidence.entry_id,
                evidence.release_url,
                evidence.reference_year,
                evidence.reference_month,
                evidence.change_jobs,
                winner,
                evidence.provenance_text,
                evidence.source_name,
                evidence.valid_wall_time_ns,
                evidence.valid_monotonic_ns,
            ),
        )
        self.conn.commit()

    def arm_precheck(
        self,
        run_id: int,
        *,
        local_utc: str,
        wall_ns: int,
        mono_ns: int,
        hostname: str,
        platform: str,
        contact_configured: bool,
    ) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO arm_precheck
            (run_id,local_utc,wall_time_ns,monotonic_ns,hostname,platform,
             clock_sync_status,flash_contact_configured) VALUES (?,?,?,?,?,?,?,?)""",
            (
                run_id,
                local_utc,
                wall_ns,
                mono_ns,
                hostname,
                platform,
                "UNKNOWN_NOT_PORTABLY_AVAILABLE",
                int(contact_configured),
            ),
        )
        self.conn.commit()

    def source_attempt(self, run_id: int, attempt: SourceAttempt) -> None:
        self.conn.execute(
            """INSERT INTO source_attempts
            (run_id,phase,source_name,source_url,request_number,
             request_started_wall_ns,request_started_monotonic_ns,
             first_byte_wall_ns,first_byte_monotonic_ns,body_complete_wall_ns,
             body_complete_monotonic_ns,parse_complete_wall_ns,
             parse_complete_monotonic_ns,parser_runtime_us,http_status,http_date,
             age,etag,last_modified,cache_control,payload,payload_length,payload_sha256,
             parsed_value,reference_year,reference_month,validation_result,rejection_reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id,
                attempt.phase,
                attempt.source_name,
                attempt.source_url,
                attempt.request_number,
                attempt.request_started_wall_ns,
                attempt.request_started_monotonic_ns,
                attempt.first_byte_wall_ns,
                attempt.first_byte_monotonic_ns,
                attempt.body_complete_wall_ns,
                attempt.body_complete_monotonic_ns,
                attempt.parse_complete_wall_ns,
                attempt.parse_complete_monotonic_ns,
                attempt.parser_runtime_us,
                attempt.http_status,
                attempt.http_date,
                attempt.age,
                attempt.etag,
                attempt.last_modified,
                attempt.cache_control,
                attempt.payload,
                len(attempt.payload),
                attempt.payload_sha256,
                attempt.parsed_value,
                attempt.reference_year,
                attempt.reference_month,
                attempt.validation_result,
                attempt.rejection_reason,
            ),
        )
        self.conn.commit()

    def invalidate_executions(
        self,
        run_id: int,
        *,
        reason: str = "OFFICIAL_SOURCE_CONFLICT: execution-validity invalidated",
    ) -> None:
        self.conn.execute(
            """UPDATE paper_executions SET execution_valid=0,
            reason=reason || '; ' || ?
            WHERE run_id=? AND execution_valid=1""",
            (reason, run_id),
        )
        self.conn.commit()

    def observation(self, values: tuple[Any, ...]) -> None:
        self.conn.execute(
            """INSERT INTO market_observations
            (run_id,phase,wall_time_ns,monotonic_ns,iso_utc,asset_token,market_id,bracket,
             token_outcome,bid,ask,bid_size,ask_size,known_payout,execution_side,price,
             available_shares,capital_required,gross_edge_per_share,fees,conservative_slippage,
             net_edge_per_share,net_executable_dollars,return_on_capital,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )

    def paper_execution(self, values: tuple[Any, ...]) -> None:
        self.conn.execute(
            """INSERT INTO paper_executions
            (run_id,is_rehearsal,token,market_id,bracket,side,decision_wall_time_ns,
             decision_monotonic_ns,decision_iso_utc,execution_wall_time_ns,execution_monotonic_ns,
             execution_iso_utc,price,shares,displayed_shares,capital,gross_dollars,costs,
             net_dollars,return_on_capital,reason)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            values,
        )

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        error: str | None,
        summary: dict[str, Any] | None,
    ) -> None:
        if summary is not None:
            missing = set(SUMMARY_COLUMNS) - set(summary)
            if missing:
                raise ValueError(f"trial summary missing columns: {sorted(missing)}")
            placeholders = ",".join("?" for _ in SUMMARY_COLUMNS)
            self.conn.execute(
                f"INSERT OR REPLACE INTO trial_summary ({','.join(SUMMARY_COLUMNS)}) VALUES ({placeholders})",
                tuple(summary[column] for column in SUMMARY_COLUMNS),
            )
        self.conn.execute(
            "UPDATE trial_runs SET status=?,error=? WHERE id=?", (status, error, run_id)
        )
        self.conn.commit()

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
