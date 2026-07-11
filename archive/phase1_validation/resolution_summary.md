# Phase 1 Historical Resolution Summary

- Resolver write used the existing CLI with `--limit 5`; earlier IDs 2, 3, and 4 were checked and remained OPEN.
- Trade 6: `CLOSED`, outcome `YES`, Brier `0.3364`, P&L `-$100.00`, lookup `snapshot_id`.
- Trade 13: `CLOSED`, outcome `NO`, Brier `0.0004`, P&L `+$47.167`, lookup `snapshot_id`.
- Remaining OPEN IDs: `2, 3, 4, 14`.
- Only rows 6 and 13 changed; the SQLite schema, watchlist, and thresholds did not change.
