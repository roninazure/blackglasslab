CREATE TABLE IF NOT EXISTS shadow_forecasts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  forecast_key TEXT NOT NULL UNIQUE,
  run_id TEXT NOT NULL,
  timestamp_utc TEXT NOT NULL,
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  question TEXT NOT NULL,
  category TEXT NOT NULL,
  market_probability REAL NOT NULL CHECK (market_probability BETWEEN 0.0 AND 1.0),
  model_probability REAL NOT NULL CHECK (model_probability BETWEEN 0.0 AND 1.0),
  side TEXT NOT NULL CHECK (side IN ('YES', 'NO')),
  absolute_edge REAL NOT NULL CHECK (absolute_edge >= 0.0),
  opportunity_score REAL,
  quality_score REAL,
  grade TEXT,
  contract_validity TEXT NOT NULL,
  opportunity_quality TEXT NOT NULL,
  model_edge TEXT NOT NULL,
  production_decision TEXT NOT NULL,
  rejection_reason TEXT,
  temporal_validation TEXT,
  skeptic_result TEXT,
  market_end_date TEXT,
  time_to_resolution_days REAL,
  llm_used INTEGER NOT NULL DEFAULT 0 CHECK (llm_used IN (0, 1)),
  model_name TEXT,
  status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'RESOLVED')),
  eventual_outcome TEXT CHECK (eventual_outcome IS NULL OR eventual_outcome IN ('YES', 'NO')),
  resolved_at_utc TEXT,
  brier_score REAL,
  hypothetical_win INTEGER CHECK (hypothetical_win IS NULL OR hypothetical_win IN (0, 1)),
  hypothetical_pnl REAL,
  roi REAL,
  holding_period_days REAL,
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_shadow_forecasts_run_id
  ON shadow_forecasts(run_id);
CREATE INDEX IF NOT EXISTS idx_shadow_forecasts_market_status
  ON shadow_forecasts(venue, market_id, status);
CREATE INDEX IF NOT EXISTS idx_shadow_forecasts_timestamp
  ON shadow_forecasts(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_shadow_forecasts_status
  ON shadow_forecasts(status);

CREATE TABLE IF NOT EXISTS shadow_threshold_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  shadow_forecast_id INTEGER NOT NULL REFERENCES shadow_forecasts(id) ON DELETE CASCADE,
  bucket_label TEXT NOT NULL,
  threshold REAL NOT NULL CHECK (threshold >= 0.0),
  is_production_threshold INTEGER NOT NULL DEFAULT 0 CHECK (is_production_threshold IN (0, 1)),
  qualifies INTEGER NOT NULL CHECK (qualifies IN (0, 1)),
  hypothetical_stake_usd REAL NOT NULL CHECK (hypothetical_stake_usd > 0.0),
  hypothetical_win INTEGER CHECK (hypothetical_win IS NULL OR hypothetical_win IN (0, 1)),
  hypothetical_pnl REAL,
  roi REAL,
  holding_period_days REAL,
  UNIQUE (shadow_forecast_id, bucket_label)
);

CREATE INDEX IF NOT EXISTS idx_shadow_threshold_bucket
  ON shadow_threshold_results(bucket_label, qualifies);

CREATE TRIGGER IF NOT EXISTS trg_shadow_forecasts_immutable_entry
BEFORE UPDATE OF
  forecast_key, run_id, timestamp_utc, venue, market_id, slug, question,
  category, market_probability, model_probability, side, absolute_edge,
  opportunity_score, quality_score, grade, contract_validity,
  opportunity_quality, model_edge, production_decision, rejection_reason,
  temporal_validation, skeptic_result, market_end_date,
  time_to_resolution_days, llm_used, model_name, metadata
ON shadow_forecasts
BEGIN
  SELECT RAISE(ABORT, 'shadow forecast entry fields are immutable');
END;
