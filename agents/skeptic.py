from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional
import random


@dataclass
class SkepticOutput:
    side: str
    confidence: float
    rationale: str


def _seed_from_state(state: Optional[Dict[str, Any]]) -> int:
    if not state:
        return 1337
    try:
        return int(state.get("seed", 1337))
    except Exception:
        return 1337


def _mode_from_state(state: Optional[Dict[str, Any]]) -> str:
    if not state:
        return "always_opposite"
    return str(state.get("mode", "always_opposite"))


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))


# ---------------------------------------------------------------------------
# LLM challenge mode (Claude — Phase 3)
# ---------------------------------------------------------------------------

def _claude_available() -> bool:
    try:
        from llm.claude_client import claude_enabled
        return claude_enabled()
    except Exception:
        return False


def _run_llm_challenge(
    market: Dict[str, Any],
    operator_side: str,
    operator_conf: float,
    operator_rationale: str,
) -> SkepticOutput:
    """
    Claude-powered skeptic: given the operator's call, challenge it with
    independent reasoning and return a counter-probability estimate.
    """
    from llm.claude_client import _get_client, _load_dotenv
    import json, os, re

    _load_dotenv()
    client = _get_client()
    model = os.environ.get("BGL_LLM_MODEL", "claude-haiku-4-5-20251001").strip()

    question = str(market.get("question") or market.get("market_id") or "")
    op_p_yes = float(operator_conf) if operator_side.upper() == "YES" else float(1.0 - operator_conf)

    system_prompt = (
        "You are a devil's advocate in a prediction market debate. "
        "An operator has made a forecast. Your job is to challenge it rigorously — "
        "find weaknesses, alternative outcomes, and overlooked risks. "
        "Then give your OWN independent probability estimate. "
        "Return ONLY valid JSON:\n"
        '{"p_yes": <float 0.01-0.99>, "confidence": <float 0.50-0.90>, "challenge": "<1-2 sentences of your counter-argument>"}\n'
        "No markdown, no extra text."
    )

    user_prompt = (
        f"Market question: {question}\n\n"
        f"Operator forecast: {operator_side} with confidence {operator_conf:.2f} "
        f"(implies P_YES={op_p_yes:.3f})\n"
        f"Operator rationale: {operator_rationale[:300]}\n\n"
        "Challenge this forecast. What is the operator missing or overweighting? "
        "What is YOUR independent P_YES estimate? Return JSON only."
    )

    resp = client.messages.create(
        model=model,
        max_tokens=350,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw).rstrip("` \n")

    data = json.loads(raw)
    p_yes = max(0.01, min(0.99, float(data["p_yes"])))
    confidence = max(0.50, min(0.90, float(data.get("confidence", 0.65))))
    challenge = str(data.get("challenge", ""))

    side = "YES" if p_yes >= 0.5 else "NO"
    conf = float(p_yes) if side == "YES" else float(1.0 - p_yes)
    conf = max(0.50, min(0.99, conf))

    return SkepticOutput(
        side=side,
        confidence=conf,
        rationale=f"[LLM_SKEPTIC] {challenge} (p_yes={p_yes:.3f})",
    )


# ---------------------------------------------------------------------------
# Main run function
# ---------------------------------------------------------------------------

def run(
    market: Dict[str, Any],
    operator_side: str,
    operator_conf: float,
    operator_rationale: str,
    skeptic_state: Optional[Dict[str, Any]] = None,
) -> SkepticOutput:
    """
    Skeptic agent v0.7 — adds LLM challenge mode (Phase 3).
    Modes:
      - llm_challenge   (Phase 3) Claude challenges the operator's reasoning
      - always_opposite (default) mechanical flip
      - always_no
      - mirror_confidence
    """
    seed = _seed_from_state(skeptic_state)
    mode = _mode_from_state(skeptic_state)

    # LLM challenge mode
    if mode == "llm_challenge" and _claude_available():
        try:
            return _run_llm_challenge(market, operator_side, operator_conf, operator_rationale)
        except Exception as e:
            # Graceful fallback
            mode = "always_opposite"
            fallback_note = f"[LLM_FAIL:{str(e)[:80]}] "
    else:
        fallback_note = ""

    rng = random.Random(seed)
    op_side = str(operator_side).upper()
    op_conf = _clamp01(operator_conf)

    if mode == "always_no":
        side = "NO"
        conf = max(0.50, min(0.75, 0.55 + 0.10 * rng.random()))
        rationale = "Mode=always_no. Defaulting to NO with moderate confidence."

    elif mode == "mirror_confidence":
        side = "NO" if op_side == "YES" else "YES"
        conf = _clamp01(op_conf)
        rationale = (
            f"Mode=mirror_confidence. Flipping operator side ({op_side}) while mirroring confidence ({op_conf:.2f})."
        )

    else:
        # always_opposite (default)
        side = "NO" if op_side == "YES" else "YES"
        conf = _clamp01(0.50 + (op_conf - 0.50) * 0.85)
        rationale = (
            f"Mode=always_opposite. Operator said {op_side} ({op_conf:.2f}). "
            f"Skeptic takes the other side with damped confidence ({conf:.2f})."
        )

    if operator_rationale:
        rationale += f" Operator rationale (ref): {operator_rationale[:180]}"

    return SkepticOutput(
        side=side,
        confidence=float(conf),
        rationale=f"{fallback_note}{rationale}",
    )
