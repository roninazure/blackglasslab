-- v0.7: population + lineage

CREATE TABLE IF NOT EXISTS agent_population (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent_id TEXT NOT NULL,
  role TEXT NOT NULL,              -- operator|skeptic
  mode TEXT NOT NULL,
  seed INTEGER NOT NULL,
  generation INTEGER NOT NULL,
  parent_agent_id TEXT,
  mutation TEXT NOT NULL,
  created_ts_utc TEXT NOT NULL,
  is_active INTEGER NOT NULL DEFAULT 1
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_population_agentid
  ON agent_population(agent_id);

CREATE INDEX IF NOT EXISTS idx_agent_population_role_active
  ON agent_population(role, is_active);

CREATE INDEX IF NOT EXISTS idx_agent_population_gen
  ON agent_population(generation);
