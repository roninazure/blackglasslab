PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS revenue_poc_accounts (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  created_at_utc TEXT NOT NULL,
  starting_balance_usd REAL NOT NULL CHECK (starting_balance_usd > 0),
  position_size_usd REAL NOT NULL CHECK (position_size_usd > 0),
  max_open_positions INTEGER NOT NULL CHECK (max_open_positions > 0),
  max_capital_deployed_usd REAL NOT NULL CHECK (max_capital_deployed_usd > 0),
  max_category_positions INTEGER NOT NULL CHECK (max_category_positions > 0),
  min_executable_edge REAL NOT NULL CHECK (min_executable_edge BETWEEN 0 AND 1),
  daily_api_budget_usd REAL NOT NULL CHECK (daily_api_budget_usd >= 0),
  config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revenue_poc_evaluations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_key TEXT NOT NULL UNIQUE,
  state_fingerprint TEXT NOT NULL,
  source_shadow_forecast_id INTEGER REFERENCES shadow_forecasts(id),
  run_id TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  question TEXT NOT NULL,
  category TEXT NOT NULL,
  model_probability REAL NOT NULL,
  market_probability REAL NOT NULL,
  executable_bid REAL NOT NULL,
  executable_ask REAL NOT NULL,
  spread REAL NOT NULL,
  depth_usd REAL NOT NULL,
  depth_source TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('YES','NO')),
  entry_price REAL NOT NULL,
  raw_edge REAL NOT NULL,
  executable_edge REAL NOT NULL,
  expected_value_usd REAL NOT NULL,
  fee_usd REAL NOT NULL,
  slippage_usd REAL NOT NULL,
  spread_cost_usd REAL NOT NULL,
  capital_required_usd REAL NOT NULL,
  expected_holding_days REAL,
  fixed_threshold REAL NOT NULL,
  adaptive_threshold REAL NOT NULL,
  adaptive_qualifies INTEGER NOT NULL CHECK (adaptive_qualifies IN (0,1)),
  llm_used INTEGER NOT NULL CHECK (llm_used IN (0,1)),
  production_decision TEXT NOT NULL,
  production_rejection_reason TEXT,
  metadata TEXT NOT NULL,
  UNIQUE (venue, market_id, state_fingerprint)
);

CREATE TABLE IF NOT EXISTS revenue_poc_positions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_id INTEGER NOT NULL UNIQUE REFERENCES revenue_poc_evaluations(id),
  opened_at_utc TEXT NOT NULL,
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  question TEXT NOT NULL,
  category TEXT NOT NULL,
  side TEXT NOT NULL CHECK (side IN ('YES','NO')),
  entry_price REAL NOT NULL,
  model_probability REAL NOT NULL,
  size_usd REAL NOT NULL,
  fee_usd REAL NOT NULL,
  slippage_usd REAL NOT NULL,
  spread_cost_usd REAL NOT NULL,
  expected_value_usd REAL NOT NULL,
  expected_holding_days REAL,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','RESOLVED','VOID')),
  resolved_outcome TEXT CHECK (resolved_outcome IS NULL OR resolved_outcome IN ('YES','NO','VOID')),
  resolved_at_utc TEXT,
  realized_pnl_usd REAL,
  UNIQUE (venue, market_id)
);

CREATE TABLE IF NOT EXISTS revenue_poc_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  evaluation_id INTEGER REFERENCES revenue_poc_evaluations(id),
  timestamp_utc TEXT NOT NULL,
  decision TEXT NOT NULL,
  reason TEXT NOT NULL,
  expected_lost_pnl_usd REAL NOT NULL DEFAULT 0,
  details TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revenue_poc_api_daily (
  date_utc TEXT PRIMARY KEY,
  api_calls INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  estimated_cost_usd REAL NOT NULL DEFAULT 0,
  cache_hits INTEGER NOT NULL DEFAULT 0,
  calls_avoided INTEGER NOT NULL DEFAULT 0,
  markets_evaluated INTEGER NOT NULL DEFAULT 0,
  candidates INTEGER NOT NULL DEFAULT 0,
  admitted_trades INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_revenue_positions_status ON revenue_poc_positions(status);
CREATE INDEX IF NOT EXISTS idx_revenue_positions_category ON revenue_poc_positions(category,status);
CREATE INDEX IF NOT EXISTS idx_revenue_evaluations_market ON revenue_poc_evaluations(venue,market_id);
CREATE INDEX IF NOT EXISTS idx_revenue_decisions_reason ON revenue_poc_decisions(reason);

CREATE TRIGGER IF NOT EXISTS trg_revenue_evaluations_append_only
BEFORE UPDATE ON revenue_poc_evaluations
BEGIN SELECT RAISE(ABORT, 'revenue evaluations are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_revenue_positions_entry_immutable
BEFORE UPDATE OF evaluation_id,opened_at_utc,venue,market_id,question,category,side,
  entry_price,model_probability,size_usd,fee_usd,slippage_usd,spread_cost_usd,
  expected_value_usd,expected_holding_days ON revenue_poc_positions
BEGIN SELECT RAISE(ABORT, 'revenue position entry fields are immutable'); END;

CREATE TRIGGER IF NOT EXISTS trg_revenue_decisions_append_only
BEFORE UPDATE ON revenue_poc_decisions
BEGIN SELECT RAISE(ABORT, 'revenue decisions are append-only'); END;

CREATE TRIGGER IF NOT EXISTS trg_revenue_decisions_no_delete
BEFORE DELETE ON revenue_poc_decisions
BEGIN SELECT RAISE(ABORT, 'revenue decisions are append-only'); END;
