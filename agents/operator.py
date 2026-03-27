from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Any, Optional
import random


@dataclass
class AgentOutput:
    side: str          # "YES" or "NO"
    confidence: float  # 0.50 - 0.99
    rationale: str
    meta: Dict[str, Any]


# ---------------------------------------------------------------------------
# LLM mode helpers (Claude — Phase 3)
# ---------------------------------------------------------------------------

def _claude_available() -> bool:
    try:
        from llm.claude_client import claude_enabled
        return claude_enabled()
    except Exception:
        return False


def _run_llm(market: Dict[str, Any], seed: int) -> AgentOutput:
    """Call Claude to generate a probability forecast for this market."""
    from llm.claude_client import forecast_yes_probability
    from models.baseline import market_yes_price

    question = str(market.get("question") or market.get("market_id") or "")
    p_yes_market, spread, _ = market_yes_price(market)

    context = {
        "venue": "polymarket",
        "slug": market.get("market_id", ""),
        "p_yes_market": float(p_yes_market),
        "market_snapshot": {
            "question": question,
            "bestBid": market.get("bestBid"),
            "bestAsk": market.get("bestAsk"),
            "lastTradePrice": market.get("lastTradePrice"),
            "volume": market.get("volume"),
            "liquidity": market.get("liquidity"),
            "updatedAt": market.get("updatedAt"),
        },
        "policy": {"return_json_only": True, "paper_only": True},
    }

    p_yes, confidence, rationale = forecast_yes_probability(question, context)
    side = "YES" if p_yes >= 0.5 else "NO"
    # Remap p_yes → confidence in the side direction
    conf = float(p_yes) if side == "YES" else float(1.0 - p_yes)
    conf = max(0.50, min(0.99, conf))

    return AgentOutput(
        side=side,
        confidence=conf,
        rationale=f"[LLM] {rationale} (p_yes={p_yes:.3f} mkt={p_yes_market:.3f})",
        meta={
            "agent": "operator_llm_claude",
            "method": "llm_claude",
            "p_yes": p_yes,
            "p_yes_market": p_yes_market,
            "llm_confidence": confidence,
            "seed": seed,
        },
    )


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(market: Dict[str, Any], state: Optional[Dict[str, Any]] = None) -> AgentOutput:
    """
    v0.4 operator: heuristic + seeded variability, with LLM mode (Phase 3).
    State example: {"mode": "llm_claude"} or {"mode": "heuristic_yes_bias", "seed": 123}
    """
    state = state or {}
    seed = int(state.get("seed", 1337))
    mode = str(state.get("mode", "heuristic_yes_bias"))

    # LLM mode — Claude does the reasoning
    if mode == "llm_claude" and _claude_available():
        try:
            return _run_llm(market, seed)
        except Exception as e:
            # Graceful fallback to heuristic if LLM call fails
            rationale_prefix = f"[LLM_FAIL:{str(e)[:80]}] fallback→heuristic. "
        mode = "heuristic_yes_bias"  # fall through to heuristic
    else:
        rationale_prefix = ""

    # Seeded RNG ensures reproducibility per market_id + seed
    rng = random.Random(seed + (hash(market.get("market_id", "")) % 10_000))

    q = (market.get("question") or "").lower()

    # Base heuristic
    if "not" in q or "no " in q:
        side = "NO"
        conf = 0.58
        rationale = "Heuristic: negation detected; leaning NO."
    elif "will" in q or "expected" in q:
        side = "YES"
        conf = 0.62
        rationale = "Heuristic: forward-looking phrasing; leaning YES."
    else:
        side = rng.choice(["YES", "NO"])
        conf = 0.55
        rationale = "Heuristic: unclear; seeded pick."

    # Mutations: occasional flip + confidence jitter
    if mode.startswith("mutant_"):
        if rng.random() < 0.25:
            side = "NO" if side == "YES" else "YES"
            rationale += " (mutant flip)"
        conf = max(0.50, min(0.80, conf + rng.uniform(-0.06, 0.06)))

    return AgentOutput(
        side=side,
        confidence=float(max(0.50, min(0.99, conf))),
        rationale=f"{rationale_prefix}{rationale} mode={mode} seed={seed}",
        meta={"agent": "operator_v0.4", "method": "heuristic+seed", "mode": mode, "seed": seed},
    )
