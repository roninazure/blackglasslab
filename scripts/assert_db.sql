-- scripts/assert_db.sql
-- Deterministic proof queries for BlackGlassLab v0.7
-- Expects caller to substitute :RUN_ID via shell (not sqlite named params).

-- Latest run_id must exist
SELECT 'LATEST_RUN_ID', run_id
FROM runs
ORDER BY id DESC
LIMIT 1;

-- Arbiter must exist for this run_id
SELECT 'ARBITER_COUNT', COUNT(*)
FROM arbiter_runs
WHERE run_id='__RUN_ID__';

-- Show latest arbiter row for this run_id
SELECT 'ARBITER_ROW',
       id, run_id, ts_utc, consensus_side, consensus_p_yes, disagreement, winner_agent, winner_fitness
FROM arbiter_runs
WHERE run_id='__RUN_ID__'
ORDER BY id DESC
LIMIT 1;

-- Latest paper trade rows (proof)
SELECT 'PAPER_TRADES_LAST3',
       id, run_id, venue, reason, status, market_id, consensus_p_yes, disagreement, size_usd
FROM paper_trades
ORDER BY id DESC
LIMIT 3;

-- Must have a paper trade for this run_id (after live_runner --paper)
SELECT 'PAPER_TRADE_FOR_RUN_COUNT', COUNT(*)
FROM paper_trades
WHERE run_id='__RUN_ID__';

-- Show most recent paper trade for this run_id
SELECT 'PAPER_TRADE_FOR_RUN_ROW',
       id, run_id, ts_utc, venue, reason, status, market_id, side, consensus_p_yes, disagreement, size_usd
FROM paper_trades
WHERE run_id='__RUN_ID__'
ORDER BY id DESC
LIMIT 1;
