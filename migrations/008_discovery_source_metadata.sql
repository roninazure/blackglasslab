PRAGMA foreign_keys=ON;

ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN source_event_category TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN source_event_title TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN source_tags TEXT NOT NULL DEFAULT '[]';
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN source_series TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN source_type TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN policy_market_class TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN policy_rejection_reason TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN reporting_class TEXT;
ALTER TABLE revenue_poc_discovery_snapshots ADD COLUMN source_metadata TEXT NOT NULL DEFAULT '{}';
