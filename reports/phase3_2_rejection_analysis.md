# Phase 3.2 Rejection Analysis

- Timestamp: `2026-07-24T14:21:56.682634+00:00`
- Policy: `institutional_v2`
- Unique markets scanned: 5000
- Tier counts: {"BANNED": 4153, "CORE": 26, "RESEARCH": 70, "WATCH": 751}

## Root Causes

- Phase 3.1 used question-only keyword classification, leaving most active markets as novelty/other.
- Discovery used one volume-sorted market query and discarded event category/title context.
- One threshold set forced serious near-threshold markets into the same rejection path as weak markets.
- The previous malformed check treated missing terminal punctuation as malformed even when grammar was otherwise clear.

## Rejection Breakdown

| Cause | Count |
| --- | ---: |
| category_ban | 1344 |
| unknown_category | 2703 |
| missing_or_unclear_deadline | 4 |
| liquidity_below_threshold | 467 |
| volume_below_threshold | 489 |
| spread_too_wide | 443 |
| probability_out_of_band | 274 |
| weak_resolution_quality | 106 |
| malformed_question | 0 |
| duplicate_exposure | 1 |
| horizon_outside_range | 258 |
| quality_score_below_threshold | 561 |
| api_metadata_missing | 76 |

Counts overlap because one market can fail multiple quality requirements. Hard-banned classes remain separate from WATCH quality failures.

## Institutional Categories

```json
{
  "GDP/economic growth": 10,
  "corporate/regulatory events": 131,
  "crypto majors": 159,
  "geopolitics": 62,
  "legal/regulatory": 1,
  "macro/fed": 33,
  "major elections": 534,
  "novelty/other": 4043,
  "oil/gas": 27
}
```
