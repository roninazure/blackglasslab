PRAGMA foreign_keys=ON;

ALTER TABLE revenue_poc_api_calls ADD COLUMN routing_tier TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE revenue_poc_api_calls ADD COLUMN decision_changed INTEGER NOT NULL DEFAULT 0;
ALTER TABLE revenue_poc_api_calls ADD COLUMN estimated_cache_savings_usd REAL;

CREATE TABLE IF NOT EXISTS revenue_poc_budget_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timestamp_utc TEXT NOT NULL,
  date_utc TEXT NOT NULL,
  cycle_id TEXT,
  operation TEXT NOT NULL,
  model TEXT,
  routing_tier TEXT,
  status TEXT NOT NULL,
  reason TEXT,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  reserved_cost_usd REAL NOT NULL DEFAULT 0,
  actual_cost_usd REAL,
  remaining_budget_usd REAL,
  reserved_budget_usd REAL,
  modeled_ev_skipped_usd REAL NOT NULL DEFAULT 0,
  priority REAL,
  details TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_revenue_budget_events_time
  ON revenue_poc_budget_events(timestamp_utc);

CREATE TABLE IF NOT EXISTS revenue_poc_discovery_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  status TEXT NOT NULL,
  rejection_reason TEXT,
  fixed_watchlist INTEGER NOT NULL DEFAULT 0 CHECK (fixed_watchlist IN (0,1)),
  dynamic_shortlist INTEGER NOT NULL DEFAULT 0 CHECK (dynamic_shortlist IN (0,1)),
  deterministic_score REAL,
  modeled_ev_usd REAL,
  capital_required_usd REAL,
  ev_per_deployed_usd REAL,
  ev_per_lockup_day REAL,
  confidence_adjusted_ev_usd REAL,
  metadata TEXT NOT NULL DEFAULT '{}',
  UNIQUE(run_id, venue, market_id)
);
CREATE INDEX IF NOT EXISTS idx_revenue_discovery_market
  ON revenue_poc_discovery_snapshots(venue,market_id,timestamp_utc);

CREATE TABLE IF NOT EXISTS revenue_poc_market_health (
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'ACTIVE',
  last_failure_reason TEXT,
  first_failure_at_utc TEXT,
  last_failure_at_utc TEXT,
  quarantined_at_utc TEXT,
  metadata TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (venue, market_id)
);

CREATE TABLE IF NOT EXISTS revenue_poc_shadow_thresholds (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_id INTEGER NOT NULL REFERENCES revenue_poc_evaluations(id),
  threshold_label TEXT NOT NULL,
  threshold REAL NOT NULL,
  qualifies INTEGER NOT NULL CHECK (qualifies IN (0,1)),
  modeled_ev_usd REAL NOT NULL,
  capital_required_usd REAL NOT NULL,
  expected_holding_days REAL,
  adaptive_threshold REAL,
  eventual_outcome TEXT,
  realized_pnl_usd REAL,
  observed INTEGER NOT NULL DEFAULT 0 CHECK (observed IN (0,1)),
  UNIQUE(evaluation_id, threshold_label)
);
CREATE INDEX IF NOT EXISTS idx_revenue_thresholds_label
  ON revenue_poc_shadow_thresholds(threshold_label,qualifies);

CREATE TRIGGER IF NOT EXISTS trg_revenue_budget_events_no_update
BEFORE UPDATE ON revenue_poc_budget_events
BEGIN SELECT RAISE(ABORT, 'revenue budget events are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_budget_events_no_delete
BEFORE DELETE ON revenue_poc_budget_events
BEGIN SELECT RAISE(ABORT, 'revenue budget events cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_discovery_no_update
BEFORE UPDATE ON revenue_poc_discovery_snapshots
BEGIN SELECT RAISE(ABORT, 'revenue discovery snapshots are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_discovery_no_delete
BEFORE DELETE ON revenue_poc_discovery_snapshots
BEGIN SELECT RAISE(ABORT, 'revenue discovery snapshots cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_thresholds_no_update
BEFORE UPDATE ON revenue_poc_shadow_thresholds
BEGIN SELECT RAISE(ABORT, 'revenue threshold experiments are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_thresholds_no_delete
BEFORE DELETE ON revenue_poc_shadow_thresholds
BEGIN SELECT RAISE(ABORT, 'revenue threshold experiments cannot be deleted'); END;
