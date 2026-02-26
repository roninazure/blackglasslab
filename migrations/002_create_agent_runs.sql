-- v0.5: normalized per-agent results
CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL,
  agent_name TEXT NOT NULL,
  side TEXT NOT NULL,
  conf REAL NOT NULL,
  rationale TEXT NOT NULL,
  brier REAL NOT NULL,
  reward REAL NOT NULL,
  score REAL NOT NULL,
  notes TEXT NOT NULL,
  ts_utc TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_runid ON agent_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent ON agent_runs(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_runs_runid_agent ON agent_runs(run_id, agent_name);
