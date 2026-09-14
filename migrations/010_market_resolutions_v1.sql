CREATE TABLE IF NOT EXISTS market_resolutions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resolution_key TEXT NOT NULL UNIQUE,
  venue TEXT NOT NULL,
  market_id TEXT NOT NULL,
  resolved_outcome TEXT NOT NULL CHECK (resolved_outcome IN ('YES', 'NO')),
  resolved_at_utc TEXT NOT NULL,
  resolution_source TEXT NOT NULL,
  source_reference TEXT,
  recorded_at_utc TEXT NOT NULL,
  provenance_metadata TEXT NOT NULL DEFAULT '{}',
  UNIQUE (venue, market_id)
);

CREATE INDEX IF NOT EXISTS idx_market_resolutions_identity
  ON market_resolutions(venue, market_id);

CREATE TRIGGER IF NOT EXISTS trg_market_resolutions_no_update
BEFORE UPDATE ON market_resolutions
BEGIN
  SELECT RAISE(ABORT, 'market resolutions are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_market_resolutions_no_delete
BEFORE DELETE ON market_resolutions
BEGIN
  SELECT RAISE(ABORT, 'market resolutions cannot be deleted');
END;
