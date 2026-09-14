CREATE TABLE IF NOT EXISTS event_microstructure_snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  observation_key TEXT NOT NULL UNIQUE,
  event_label TEXT NOT NULL,
  observation_timestamp_utc TEXT NOT NULL,
  market_id TEXT NOT NULL,
  slug TEXT NOT NULL,
  venue TEXT NOT NULL,
  best_bid REAL,
  best_ask REAL,
  midpoint REAL,
  last_trade_price REAL,
  spread REAL,
  liquidity REAL,
  volume REAL,
  source_updated_at TEXT,
  fetch_status TEXT NOT NULL,
  fetch_latency_ms REAL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_event_microstructure_event_time
  ON event_microstructure_snapshots(event_label, observation_timestamp_utc);
CREATE INDEX IF NOT EXISTS idx_event_microstructure_market_time
  ON event_microstructure_snapshots(market_id, slug, observation_timestamp_utc);

CREATE TRIGGER IF NOT EXISTS trg_event_microstructure_snapshots_immutable
BEFORE UPDATE ON event_microstructure_snapshots
BEGIN
  SELECT RAISE(ABORT, 'event microstructure snapshots are append-only');
END;

CREATE TRIGGER IF NOT EXISTS trg_event_microstructure_snapshots_no_delete
BEFORE DELETE ON event_microstructure_snapshots
BEGIN
  SELECT RAISE(ABORT, 'event microstructure snapshots are append-only');
END;
