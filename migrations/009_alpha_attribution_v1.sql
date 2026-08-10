PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS revenue_poc_attribution_entries (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_id INTEGER NOT NULL UNIQUE REFERENCES revenue_poc_evaluations(id),
  recorded_at_utc TEXT NOT NULL,
  strategy_id TEXT NOT NULL,
  strategy_version TEXT NOT NULL,
  source_id TEXT NOT NULL,
  source_type TEXT NOT NULL,
  event_family_id TEXT NOT NULL,
  category TEXT NOT NULL,
  horizon_bucket TEXT NOT NULL CHECK (horizon_bucket IN ('FAST','WEEKLY','MONTHLY','LONG','UNKNOWN')),
  entry_benchmark_price REAL NOT NULL,
  entry_benchmark_source TEXT NOT NULL,
  attribution_basis_version TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS revenue_poc_attribution_completions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entry_id INTEGER NOT NULL UNIQUE REFERENCES revenue_poc_attribution_entries(id),
  position_id INTEGER NOT NULL UNIQUE REFERENCES revenue_poc_positions(id),
  recorded_at_utc TEXT NOT NULL,
  exit_benchmark_price REAL,
  exit_benchmark_source TEXT NOT NULL,
  settlement_outcome TEXT NOT NULL CHECK (settlement_outcome IN ('YES','NO','VOID')),
  settlement_price REAL NOT NULL,
  resolved_at_utc TEXT NOT NULL,
  capital_days REAL NOT NULL CHECK (capital_days >= 0),
  fees_usd REAL NOT NULL,
  slippage_usd REAL NOT NULL,
  close_classification TEXT NOT NULL,
  forecast_alpha_usd REAL NOT NULL,
  event_alpha_usd REAL NOT NULL,
  resolution_alpha_usd REAL NOT NULL,
  structural_alpha_usd REAL NOT NULL,
  realized_net_pnl_usd REAL NOT NULL,
  attribution_status TEXT NOT NULL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_revenue_alpha_entries_dimensions
  ON revenue_poc_attribution_entries(strategy_id,source_type,category,horizon_bucket);
CREATE INDEX IF NOT EXISTS idx_revenue_alpha_completions_position
  ON revenue_poc_attribution_completions(position_id);

CREATE TRIGGER IF NOT EXISTS trg_revenue_alpha_entries_no_update
BEFORE UPDATE ON revenue_poc_attribution_entries
BEGIN SELECT RAISE(ABORT, 'revenue attribution entries are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_alpha_entries_no_delete
BEFORE DELETE ON revenue_poc_attribution_entries
BEGIN SELECT RAISE(ABORT, 'revenue attribution entries cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_alpha_completions_no_update
BEFORE UPDATE ON revenue_poc_attribution_completions
BEGIN SELECT RAISE(ABORT, 'revenue attribution completions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS trg_revenue_alpha_completions_no_delete
BEFORE DELETE ON revenue_poc_attribution_completions
BEGIN SELECT RAISE(ABORT, 'revenue attribution completions cannot be deleted'); END;
