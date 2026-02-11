from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, Optional
import random

@dataclass
class ReapEvent:
    agent: str
    action: str              # e.g. "REAP_RESET"
    reason: str
    before: Dict[str, Any]
    after: Dict[str, Any]

def maybe_reap(
    agent_state: Dict[str, Any],
    agent: str,
    last_n_reward: Optional[float],
    n: int,
    threshold: float,
) -> Optional[ReapEvent]:
    """
    If last-N average reward is below threshold, reset agent config.
    """
    if last_n_reward is None:
        return None
    if last_n_reward >= threshold:
        return None

    before = dict(agent_state.get(agent, {}))

    new_seed = random.randint(1, 1_000_000)
    new_mode = f"mutant_{new_seed % 9}"

    agent_state[agent] = {"mode": new_mode, "seed": new_seed}

    reason = f"last_{n}_avg_reward={last_n_reward:.3f} < threshold={threshold:.3f}"
    return ReapEvent(agent=agent, action="REAP_RESET", reason=reason, before=before, after=agent_state[agent])
