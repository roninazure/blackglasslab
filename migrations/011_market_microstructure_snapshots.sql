CREATE TABLE IF NOT EXISTS market_microstructure_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_key TEXT NOT NULL UNIQUE,
  timestamp_utc TEXT NOT NULL,
  cycle_id TEXT,
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  best_bid REAL,
  best_ask REAL,
  midpoint REAL,
  last_trade REAL,
  spread REAL,
  bid_executable_depth_usd REAL,
  ask_executable_depth_usd REAL,
  liquidity REAL,
  volume REAL,
  resolution_timestamp_utc TEXT,
  time_remaining_hours REAL,
  source TEXT NOT NULL,
  source_updated_at_utc TEXT
);

CREATE INDEX IF NOT EXISTS idx_microstructure_snapshots_timestamp
  ON market_microstructure_snapshots(timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_microstructure_snapshots_market
  ON market_microstructure_snapshots(venue, market_id, timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_microstructure_snapshots_cycle
  ON market_microstructure_snapshots(cycle_id, venue, market_id);

CREATE TRIGGER IF NOT EXISTS trg_microstructure_snapshots_immutable
BEFORE UPDATE ON market_microstructure_snapshots
BEGIN
  SELECT RAISE(ABORT, 'market microstructure snapshots are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_microstructure_snapshots_no_delete
BEFORE DELETE ON market_microstructure_snapshots
BEGIN
  SELECT RAISE(ABORT, 'market microstructure snapshots are append-only');
END;
