-- v0.6: add fitness + arbiter output table

-- 1) agent_runs: add fitness
ALTER TABLE agent_runs ADD COLUMN fitness REAL NOT NULL DEFAULT 0.0;

CREATE INDEX IF NOT EXISTS idx_agent_runs_fitness ON agent_runs(fitness);

-- 2) arbiter output per run
CREATE TABLE IF NOT EXISTS arbiter_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  ts_utc TEXT NOT NULL,
  consensus_side TEXT NOT NULL,
  consensus_p_yes REAL NOT NULL,
  disagreement REAL NOT NULL,
  winner_agent TEXT NOT NULL,
  winner_fitness REAL NOT NULL,
  notes TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_arbiter_runs_runid ON arbiter_runs(run_id);
