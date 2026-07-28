# Phase 1 Watchlist Rotation Summary

- Acceptance: **PASS**
- Unique markets covered: `25 / 25`
- Aggregate fetch attempts: `14`
- Aggregate LLM attempts: `8`
- Aggregate candidates: `0`
- Markets sampled more than once: `0`
- Markets never sampled: `0`
- Reconciliation mismatches: `0`
- Paper trades unchanged: `true`

## Cursor And Funnel

| Cycle | Cursor | Fetch | LLM | Diagnostics | Candidates |
|---:|:---:|---:|---:|---:|---:|
| 01 | 0 -> 5 | 5 | 4 | 5 | 0 |
| 02 | 5 -> 10 | 5 | 2 | 5 | 0 |
| 03 | 10 -> 15 | 0 | 0 | 0 | 0 |
| 04 | 15 -> 20 | 1 | 0 | 1 | 0 |
| 05 | 20 -> 0 | 3 | 2 | 3 | 0 |

## Sampled Terminal Reasons

- `existing_open_or_pending_position`: 4
- `extreme_tail`: 6
- `min_edge_abs`: 8
- `recent_infer_cooldown`: 7

## Market Outcomes By Cycle

| Market | C01 | C02 | C03 | C04 | C05 |
|---|---|---|---|---|---|
| will-bitcoin-hit-1m-before-gta-vi-872-424 | *min_edge_abs | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| scotus-accepts-sports-event-contract-case-by-july-31-2026 | *extreme_tail | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| will-china-invades-taiwan-before-gta-vi-716-644 | *min_edge_abs | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| new-rhianna-album-before-gta-vi-926 | *min_edge_abs | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| new-playboi-carti-album-before-gta-vi-421 | *min_edge_abs | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| will-gpt-6-be-released | not_selected_in_batch | *min_edge_abs | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| will-paul-reevs-be-the-republican-nominee-for-az-01 | not_selected_in_batch | *extreme_tail | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| will-matt-gress-be-the-republican-nominee-for-az-01 | not_selected_in_batch | *extreme_tail | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| will-jay-feely-be-the-republican-nominee-for-az-05 | not_selected_in_batch | *extreme_tail | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| will-cori-bush-be-the-democratic-nominee-for-mo-01 | not_selected_in_batch | *min_edge_abs | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch |
| blue-wave-in-2026 | existing_open_or_pending_position | existing_open_or_pending_position | *existing_open_or_pending_position | existing_open_or_pending_position | existing_open_or_pending_position |
| us-recession-by-end-of-2026 | existing_open_or_pending_position | existing_open_or_pending_position | *existing_open_or_pending_position | existing_open_or_pending_position | existing_open_or_pending_position |
| fed-emergency-rate-cut-before-2027 | existing_open_or_pending_position | existing_open_or_pending_position | *existing_open_or_pending_position | existing_open_or_pending_position | existing_open_or_pending_position |
| will-no-fed-rate-cuts-happen-in-2026 | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown | not_selected_in_batch | not_selected_in_batch |
| will-1-fed-rate-cut-happen-in-2026 | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown | not_selected_in_batch | not_selected_in_batch |
| will-2-fed-rate-cuts-happen-in-2026 | existing_open_or_pending_position | existing_open_or_pending_position | existing_open_or_pending_position | *existing_open_or_pending_position | existing_open_or_pending_position |
| will-3-fed-rate-cuts-happen-in-2026 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown | not_selected_in_batch |
| will-4-fed-rate-cuts-happen-in-2026 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown | not_selected_in_batch |
| will-5-fed-rate-cuts-happen-in-2026 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown | not_selected_in_batch |
| will-tarcisio-de-frietas-win-the-2026-brazilian-presidential-election | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *extreme_tail | not_selected_in_batch |
| will-eduardo-bolsonaro-win-the-2026-brazilian-presidential-election | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *extreme_tail |
| will-the-us-confirm-that-aliens-exist-before-2027-789-924-249 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown |
| will-the-us-invade-iran-before-2027 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *recent_infer_cooldown |
| will-china-invade-taiwan-before-2027 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *min_edge_abs |
| will-the-iranian-regime-fall-by-the-end-of-2026 | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | not_selected_in_batch | *min_edge_abs |

`*` marks the cycle in which the market was selected by the rotating batch cursor.
