# Legacy Review Queue

Items here are not deleted yet. Each has evidence of overlap or non-use, but removal still needs a human decision.

| Path | Evidence of non-use | Risk of removal | Recommendation |
|---|---|---|---|
| `orchestrator.py` | Current infer daemon uses `live_runner.py`; arbiter/swarm path is not part of unattended runtime. | Medium. Could still be useful as a manual swarm reference. | Keep until ownership is decided; do not delete. |
| `agents/arbiter.py` | No imports, shell references, or tests; arbiter logic is implemented inline elsewhere. | Low. | Safe candidate for removal after one more review pass. |
| `agents/{operator,skeptic,auditor,reaper,evolver}.py` | Legacy swarm path referenced by `orchestrator.py`, not by current daemon. | Medium. Hidden operator workflows may still depend on them. | Keep until the legacy orchestrator is formally retired. |
| `reporting/{eval_live,leaderboard,paper_dashboard}.py` | Standalone reporting CLIs overlap with `dashboard/app.py`, `watch_resolutions.py`, and calibration output. | Medium. Removing them would break manual workflows. | Keep for now; reconcile or deprecate explicitly. |
| `scoring/` | Used only by the legacy arbiter/auditor path. | Low to medium. | Retain until the legacy swarm path is either removed or promoted. |
| `scripts/assert_db.sql` | No runtime caller found. | Low. | Keep until schema-owner conventions are documented. |
| `scripts/void_trades.py` | Destructive repair tool, manually invoked only. | Medium. | Keep; do not remove while historical repair remains a supported task. |
| `adapters/kalshi_adapter.py` | Registry stub, not part of the current Polymarket runtime. | Low. | Keep as a placeholder unless the venue is formally out of scope. |
| `migrations/*.sql` | Not executed by runtime code; schema lineage only. | Medium. | Keep until migration ownership is documented. |
| `swarm_edge_flyer.pdf` / `swarm_edge_thumb.png` | Generated marketing assets, not runtime inputs. | Low. | Keep only if the collateral is intentionally tracked. |
